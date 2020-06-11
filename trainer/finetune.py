# Copyright 2020 Google LLC

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     https://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Script for Running, Training, and Evaluating E2E Convrec Experiments."""

# SETUP
print("Installing dependencies...")
import functools
import os
import time
import warnings
import tensorflow.compat.v1 as tf
import tensorflow_datasets as tfds
import t5
from contextlib import contextmanager
import logging as py_logging
import gzip
import json
import os
import tensorboard as tb
import random
import nltk
import sacrebleu
from absl import app
from absl import flags
from trainer import preprocessing

FLAGS = flags.FLAGS
flags.DEFINE_integer('steps', 6000, "Finetuning training steps.")
flags.DEFINE_enum("size", "base", ["small", "base", "large", "3B", "11B"], "model size")
flags.DEFINE_string("name", "default", "name/description of model  version")
flags.DEFINE_enum("mode", "all", ["train", "evaluate", "all"], "run mode: train, evaluate, or all")

INPUT_LENGTH = 512 #1033 - longest training input for redial
TARGET_LENGTH = 128 #159 - longest trainin target for redial

warnings.filterwarnings("ignore", category=DeprecationWarning)

BASE_DIR = "gs://e2e_central"
DATA_DIR = os.path.join(BASE_DIR, "data")
MODELS_DIR = os.path.join(BASE_DIR, "models")

# Public GCS path for T5 pre-trained model checkpoints
BASE_PRETRAINED_DIR = "gs://t5-data/pretrained_models"
PRETRAINED_DIR = ""
MODEL_DIR = ""

# Locations for reading and writing Redial data
RD_JSONL_DIR = "gs://e2e_central/data/redial/"
RD_SPLIT_FNAMES = {
    "train": "rd-train-formatted.jsonl",
    "validation": "rd-test-formatted.jsonl"
}
rd_counts_path = os.path.join(DATA_DIR, "rd-counts.json")
rd_tsv_path = {
    "train": os.path.join(DATA_DIR, "rd-train.tsv"),
    "validation": os.path.join(DATA_DIR, "rd-validation.tsv")
}

def tf_verbosity_level(level):
  """Changes verbosity level."""
  og_level = tf.logging.get_verbosity()
  tf.logging.set_verbosity(level)
  yield
  tf.logging.set_verbosity(og_level)

def _prediction_file_to_ckpt(path):
  """Extract the global step from a prediction filename."""
  return int(path.split("_")[-2])

def load_predictions(task_name):
  """Loads the most recent predictions in as ([(input, target, pred)], step)."""
  # Grab the dataset for this task.
  ds = t5.data.TaskRegistry.get(task_name).get_dataset(
      split="validation",
      sequence_length={"inputs": INPUT_LENGTH, "targets": TARGET_LENGTH},
      shuffle=False)

  # Grab the paths of all logged predictions.
  prediction_files = tf.io.gfile.glob(
      os.path.join(
          MODEL_DIR,
          "validation_eval/%s_*_predictions" % task_name))

  # Get most recent prediction file by sorting by their step.
  latest_prediction_file = sorted(
      prediction_files, key=_prediction_file_to_ckpt)[-1]

  checkpoint_step =_prediction_file_to_ckpt(latest_prediction_file)

  # Collect (inputs, targets, prediction) from the dataset and predictions file
  results = []
  with tf.io.gfile.GFile(latest_prediction_file) as preds:
    for ex, pred in zip(tfds.as_numpy(ds), preds):
      results.append((tf.compat.as_text(ex["inputs_plaintext"]),
                      tf.compat.as_text(ex["targets_plaintext"]),
                      pred.strip()))
  return (results, checkpoint_step)

def save_metrics(task_name):
  """Prints and saves metrics for the most recent checkpoint."""
  results, checkpoint_step = load_predictions(task_name)
  predictions = [line[2] for line in results]
  targets = [line[1] for line in results]

  hyp = list(map(lambda x: x.split(), predictions))
  ref = list(map(lambda x: [x.split()], targets))

  nltk_bs = nltk.translate.bleu_score.corpus_bleu(list_of_references=ref, hypotheses=hyp)
  sb_bs = str(sacrebleu.corpus_bleu(predictions, [targets]))

  print("NLTK BLEU SCORE: {:f}, SACREBLEU BLEU SCORE: {:s}, CHECKPOINT: {:d}".format(nltk_bs, sb_bs, checkpoint_step))
  # Writes to $MODEL_DIR$/validation_eval/metrics$CHECKPOINT_NUMBER.json
  metrics_path = os.path.join(
          MODEL_DIR,
          "validation_eval/metrics" + str(checkpoint_step) + ".json")
  json.dump({"nltk_bleu_score" : nltk_bs, "sacrebleu_blue_score" : sb_bs, "recall@1" : 0}, tf.io.gfile.GFile(metrics_path, "w"))


def main(argv):
  """Main method for fintuning: builds, trains, and evaluates t5."""
  tf.disable_v2_behavior()
  tf.compat.v1.enable_eager_execution()

  global PRETRAINED_DIR
  global MODEL_DIR
  PRETRAINED_DIR = os.path.join(BASE_PRETRAINED_DIR, FLAGS.size)
  MODEL_DIR = os.path.join(MODELS_DIR, FLAGS.size, FLAGS.name)

  print("--------DETECTING TPUs----")
  try:
    TPU_TOPOLOGY = "2x2"
    tpu = tf.distribute.cluster_resolver.TPUClusterResolver()  # TPU detection
    TPU_ADDRESS = tpu.get_master()
    print('Running on TPU:', TPU_ADDRESS)
  except ValueError:
    raise BaseException('ERROR: Not connected to a TPU runtime')

  # load or build data
  if tf.io.gfile.exists(rd_counts_path):
    print("TSV's Found")
    # Used cached data and counts.
    tf.logging.info("Loading Redial from cache.")
    num_rd_examples = json.load(tf.io.gfile.GFile(rd_counts_path))
  else:
    print("TSV's Not Found")
    # Create TSVs and get counts.
    tf.logging.info("Generating Redial TSVs.")
    num_rd_examples = {}
    for split, fname in RD_SPLIT_FNAMES.items():
      print(os.path.join(RD_JSONL_DIR, fname))
      num_rd_examples[split] = preprocessing.rd_jsonl_to_tsv(
          os.path.join(RD_JSONL_DIR, fname), rd_tsv_path[split])
    json.dump(num_rd_examples, tf.io.gfile.GFile(rd_counts_path, "w"))

  t5.data.TaskRegistry.add(
      "rd_recommendations",
      # Supply a function which returns a tf.data.Dataset.
      dataset_fn=preprocessing.rd_dataset_fn,
      splits=["train", "validation"],
      # Supply a function which preprocesses text from the tf.data.Dataset.
      text_preprocessor=[preprocessing.conversation_preprocessor],
      # Use the same vocabulary that we used for pre-training.
      sentencepiece_model_path=t5.data.DEFAULT_SPM_PATH,
      # Lowercase targets before computing metrics.
      postprocess_fn=t5.data.postprocessors.lower_text, 
      # We'll use accuracy as our evaluation metric.
      metric_fns=[t5.evaluation.metrics.accuracy, t5.evaluation.metrics.bleu],
      # Not required, but helps for mixing and auto-caching.
      num_input_examples=num_rd_examples)

  # Set parallelism and batch size to fit on v2-8 TPU (if possible).
  # Limit number of checkpoints to fit within 5GB (if possible).
  model_parallelism, train_batch_size, keep_checkpoint_max = {
      "small": (1, 256, 16),
      "base": (2, 128, 8),
      "large": (8, 64, 4),
      "3B": (8, 16, 1),
      "11B": (8, 16, 1)}[FLAGS.size]

  tf.io.gfile.makedirs(MODEL_DIR)
  # The models from the t5 paper paper are based on the Mesh Tensorflow Transformer.
  model = t5.models.MtfModel(
      model_dir=MODEL_DIR,
      tpu=TPU_ADDRESS,
      tpu_topology=TPU_TOPOLOGY,
      model_parallelism=model_parallelism,
      batch_size=train_batch_size,
      sequence_length={"inputs": INPUT_LENGTH, "targets": TARGET_LENGTH},
      learning_rate_schedule=0.003,
      save_checkpoints_steps=5000,
      keep_checkpoint_max=keep_checkpoint_max,
      iterations_per_loop=100,
  )

  FINETUNE_STEPS = FLAGS.steps #@param {type: "integer"}

  if FLAGS.mode == "all" or FLAGS.mode == "train":
    model.finetune(
        mixture_or_task_name="rd_recommendations",
        pretrained_model_dir=PRETRAINED_DIR,
        finetune_steps=FINETUNE_STEPS
    )
  tf.compat.v1.disable_eager_execution()

  # Evaluate and save predictions
  if FLAGS.mode == "all" or FLAGS.mode == "evaluate":
    model.batch_size = train_batch_size * 4 # a larger batch size requires less memory.
    model.eval(
        mixture_or_task_name="rd_recommendations",
        checkpoint_steps="all"
    )

    save_metrics("rd_recommendations")


  # Export the SavedModel
  export_dir = os.path.join(MODEL_DIR, "export")

  model.batch_size = 1 # make one prediction per call
  saved_model_path = model.export(
      export_dir,
      checkpoint_step=-1,  # use most recent
      beam_size=1,  # no beam search
      temperature=1.0,  # sample according to predicted distribution
  )
  print("Model saved to:", saved_model_path)

if __name__ == "__main__":
  app.run(main)