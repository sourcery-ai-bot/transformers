#!/usr/bin/env python
# coding=utf-8
# Copyright The HuggingFace Team and The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""

"""
import argparse
import logging
import os
import sys
from datetime import datetime

import numpy as np
import torch

import onnxruntime
import transformers
from bart_onnx.generation_onnx import BARTBeamSearchGenerator
from bart_onnx.reduce_onnx_size import remove_dup_initializers
from transformers import BartForConditionalGeneration, BartTokenizer


logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s |  [%(filename)s:%(lineno)d] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=os.environ.get("LOGLEVEL", "INFO").upper(),
    stream=sys.stdout,
)

logger = logging.getLogger(__name__)

model_dict = {"facebook/bart-base": BartForConditionalGeneration}
tokenizer_dict = {"facebook/bart-base": BartTokenizer}


def parse_args():
    parser = argparse.ArgumentParser(description="Finetune a transformers model on a text classification task")
    parser.add_argument(
        "--validation_file", type=str, default=None, help="A csv or a json file containing the validation data."
    )
    parser.add_argument(
        "--max_length",
        type=int,
        default=5,
        help=("The maximum total input sequence length after tokenization."),
    )
    parser.add_argument(
        "--num_beams",
        type=int,
        default=None,
        help="Number of beams to use for evaluation. This argument will be "
        "passed to ``model.generate``, which is used during ``evaluate`` and ``predict``.",
    )
    parser.add_argument(
        "--model_name_or_path",
        type=str,
        help="Path to pretrained model or model identifier from huggingface.co/models.",
        required=True,
    )
    parser.add_argument(
        "--config_name",
        type=str,
        default=None,
        help="Pretrained config name or path if not the same as model_name",
    )
    parser.add_argument(
        "--device",
        type=str,
        default="cpu",
        help="Device where the model will be run",
    )
    parser.add_argument("--output_file_path", type=str, default=None, help="Where to store the final ONNX file.")

    return parser.parse_args()


def load_model_tokenizer(model_name, device="cpu"):
    huggingface_model = model_dict[model_name].from_pretrained(model_name).to(device)
    tokenizer = tokenizer_dict[model_name].from_pretrained(model_name)

    if model_name in ["facebook/bart-base"]:
        huggingface_model.config.no_repeat_ngram_size = 0
        huggingface_model.config.forced_bos_token_id = None
        huggingface_model.config.min_length = 0

    return huggingface_model, tokenizer


def export_and_validate_model(model, tokenizer, onnx_file_path, num_beams, max_length):
    model.eval()

    ort_sess = None
    onnx_bart = torch.jit.script(BARTBeamSearchGenerator(model))

    with torch.no_grad():
        ARTICLE_TO_SUMMARIZE = "My friends are cool but they eat too many carbs."
        inputs = tokenizer([ARTICLE_TO_SUMMARIZE], max_length=1024, return_tensors="pt").to(model.device)

        if not ort_sess:
            # Test export here.
            summary_ids = model.generate(
                inputs["input_ids"],
                attention_mask=inputs["attention_mask"],
                num_beams=num_beams,
                max_length=max_length,
                early_stopping=True,
                decoder_start_token_id=model.config.decoder_start_token_id,
            )

            torch.onnx.export(
                onnx_bart,
                (
                    inputs["input_ids"],
                    inputs["attention_mask"],
                    num_beams,
                    max_length,
                    model.config.decoder_start_token_id,
                ),
                onnx_file_path,
                opset_version=14,
                input_names=["input_ids", "attention_mask", "num_beams", "max_length", "decoder_start_token_id"],
                output_names=["output_ids"],
                dynamic_axes={
                    "input_ids": {0: "batch", 1: "seq"},
                    "output_ids": {0: "batch", 1: "seq_out"},
                },
                verbose=False,
                strip_doc_string=False,
                example_outputs=summary_ids,
            )

            new_onnx_file_path = remove_dup_initializers(os.path.abspath(onnx_file_path))

            ort_sess = onnxruntime.InferenceSession(new_onnx_file_path)
            ort_out = ort_sess.run(
                None,
                {
                    "input_ids": inputs["input_ids"].cpu().numpy(),
                    "attention_mask": inputs["attention_mask"].cpu().numpy(),
                    "num_beams": np.array(num_beams),
                    "max_length": np.array(max_length),
                    "decoder_start_token_id": np.array(model.config.decoder_start_token_id),
                },
            )

            np.testing.assert_allclose(summary_ids.cpu().numpy(), ort_out[0], rtol=1e-3, atol=1e-3)

            print("========= Pass - Results are matched! =========")


def main():
    args = parse_args()
    local_device = None
    # Make one log on every process with the configuration for debugging.
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        level=logging.INFO,
    )

    logger.setLevel(logging.ERROR)
    transformers.utils.logging.set_verbosity_error()

    if args.model_name_or_path:
        model, tokenizer = load_model_tokenizer(args.model_name_or_path, local_device)
    else:
        raise ValueError("Make sure that model name has been passed")

    if model.config.decoder_start_token_id is None:
        raise ValueError("Make sure that `config.decoder_start_token_id` is correctly defined")

    if args.device:
        if args.device == "cuda" and not torch.cuda.is_available():
            raise ValueError("CUDA is not available in this server.")

        local_device = torch.device(args.device)
    else:
        local_device = torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")

    model.to(local_device)

    local_max_length = args.max_length if args.max_length else 5
    local_num_beams = args.num_beams if args.num_beams else 4
    if args.output_file_path:
        output_name = args.output_file_path
    else:
        output_name = f"onnx_model_{datetime.now().utcnow().microsecond}.onnx"

    export_and_validate_model(model, tokenizer, output_name, local_num_beams, local_max_length)

    logger.info("***** Running export *****")


if __name__ == "__main__":
    main()
