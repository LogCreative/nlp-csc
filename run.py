from __future__ import absolute_import, division, print_function
import argparse
import logging
import glob
import os
import random
import math
import copy
import json
import numpy as np
import torch
from torch.utils.data import DataLoader, TensorDataset, IterableDataset
from tqdm.auto import tqdm, trange
from transformers import AutoTokenizer, get_scheduler
from accelerate import Accelerator
from autocsc import *
import jsonlines

import time
import atexit
import functools
from torch.utils.tensorboard import SummaryWriter

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(name)s -   %(message)s",
                    datefmt="%m/%d/%Y %H:%M:%S",
                    level=logging.INFO)
logger = logging.getLogger(__name__)


class InputExample(object):
    def __init__(self, guid, src, trg):
        self.guid = guid
        self.src = src
        self.trg = trg


class InputFeatures(object):
    def __init__(self, src_ids, attention_mask, trg_ids, trg_ref_ids=None):
        self.src_ids = src_ids
        self.attention_mask = attention_mask
        self.trg_ids = trg_ids
        self.trg_ref_ids = trg_ref_ids


class ConfusDataset(IterableDataset):
    """
    Processor for monolingual data to be augmented with confusion sets.
    """

    def __init__(self, list_of_files, tokenizer, max_seq_length, seed, buffer_size=10000, shuffle=True):
        super().__init__()
        self.list_of_files = glob.glob(list_of_files)
        self.list_of_files.sort()
        self.tokenizer = tokenizer
        self.max_seq_length = max_seq_length

        self.confus_dir = "confus"
        with open(os.path.join(self.confus_dir, "stroke.json"), "r") as f:
            self.stroke_confus = json.load(f)
        with open(os.path.join(self.confus_dir, "pinyin_sim.json"), "r") as f:
            self.pinyin_sim_confus = json.load(f)
        with open(os.path.join(self.confus_dir, "pinyin_sam.json"), "r") as f:
            self.pinyin_sam_confus = json.load(f)
        with open(os.path.join(self.confus_dir, "word_freq.txt"), "r") as f:
            self.word_list = [line.strip() for line in f.readlines()]

        self.buffer_size = buffer_size
        self.generator = np.random.default_rng(seed=seed)
        self.shuffle = shuffle
    
    def vectorize(self, src, trg):
        tokenizer = self.tokenizer
        src_ids = self.tokenizer(src,
                                 max_length=self.max_seq_length // 2 - 2,
                                 truncation=True,
                                 is_split_into_words=True,
                                 add_special_tokens=False).input_ids
        trg_ids = self.tokenizer(trg,
                                 max_length=self.max_seq_length // 2 - 2,
                                 truncation=True,
                                 is_split_into_words=True,
                                 add_special_tokens=False).input_ids
        input_ids = [tokenizer.cls_token_id] + src_ids + [tokenizer.sep_token_id] + [tokenizer.mask_token_id for _ in trg_ids] + [tokenizer.sep_token_id]
        label_ids = [tokenizer.cls_token_id] + src_ids + [tokenizer.sep_token_id] + trg_ids + [tokenizer.sep_token_id]
        attention_mask = [1] * len(input_ids)
        ref_ids = [tokenizer.cls_token_id] + trg_ids + [tokenizer.sep_token_id] + trg_ids + [tokenizer.sep_token_id]

        offset_length = self.max_seq_length - len(input_ids)
        if offset_length > 0:
            input_ids = input_ids + [tokenizer.pad_token_id] * offset_length
            attention_mask = attention_mask + [0] * offset_length
            label_ids = label_ids + [tokenizer.pad_token_id] * offset_length
            ref_ids = ref_ids + [tokenizer.pad_token_id] * offset_length
        input_ids, attention_mask, label_ids, ref_ids = input_ids[:self.max_seq_length], attention_mask[:self.max_seq_length], label_ids[:self.max_seq_length], ref_ids[:self.max_seq_length]

        assert len(input_ids) == self.max_seq_length
        assert len(attention_mask) == self.max_seq_length
        assert len(label_ids) == self.max_seq_length

        return torch.LongTensor(input_ids), torch.LongTensor(attention_mask), torch.LongTensor(label_ids), torch.LongTensor(ref_ids)
    
    def _read(self):
        for filename in self.list_of_files:
            with open(filename) as f:
                for line in f:
                    src = line.strip()
                    src, trg = self.confus(src)

                    yield self.vectorize(src, trg)

    def __iter__(self):
        if self.shuffle:
            return self._shuffle(self._read())
        else:
            return self._read()

    @staticmethod
    def _iter_random_indices(rng, buffer_size):
        while True:
            yield from (int(i) for i in rng.integers(0, buffer_size, size=10000))

    def _shuffle(self, ex_iterable):
        buffer_size = self.buffer_size
        rng = deepcopy(self.generator)
        indices_iterator = self._iter_random_indices(rng, buffer_size)

        mem_buffer = []
        for x in ex_iterable:
            if len(mem_buffer) == buffer_size:
                i = next(indices_iterator)
                yield mem_buffer[i]
                mem_buffer[i] = x
            else:
                mem_buffer.append(x)

        rng.shuffle(mem_buffer)
        yield from mem_buffer

    @staticmethod
    def ishan(char):
        return "\u4e00" <= char <= "\u9fff"

    def confus(self, src):
        tokens = list(src)
        all_indices = [i for i, t in enumerate(tokens) if self.ishan(t)]
        if not all_indices:
            return tokens, list(src)
        chosen = random.choice(all_indices)
        j = chosen
        rn = random.random()
        if rn < 0.4:
            if tokens[j] in self.pinyin_sam_confus:
                tokens[j] = random.choice(self.pinyin_sam_confus[tokens[j]])
            else:
                tokens[j] = random.choice(self.word_list)
        elif rn < 0.7:
            if tokens[j] in self.pinyin_sim_confus:
                tokens[j] = random.choice(self.pinyin_sim_confus[tokens[j]])
            else:
                tokens[j] = random.choice(self.word_list)
        elif rn < 0.9:
            if tokens[j] in self.stroke_confus:
                tokens[j] = random.choice(self.stroke_confus[tokens[j]])
            else:
                tokens[j] = random.choice(self.word_list)
        else:
            tokens[j] = random.choice(self.word_list)

        return tokens, list(src)


class DataProcessor:
    """
    Processor for the data set:
    a) in a .tsv format, i.e. id\tsrc\ttrg; b) separate Chinese characters from each other by spaces;
    """

    def get_train_examples(self, data_dir, filename):
        return self._create_examples(self._read(os.path.join(data_dir, filename)), "train")

    def get_dev_examples(self, data_dir, filename):
        return self._create_examples(self._read(os.path.join(data_dir, filename)), "dev")

    def get_test_examples(self, data_dir, filename):
        return self._create_examples(self._read(os.path.join(data_dir, filename)), "test")

    @staticmethod
    def _read(input_file):
        with open(input_file, "r", encoding="utf-8") as f:
            lines = []
            first_line = f.readline()   # ignore the first line
            for line in f:
                cols = line.strip().split("\t")
                if len(cols) == 3:
                    lines.append((cols[0], cols[1].split(), cols[2].split()))
                else:  # it is a test set
                    lines.append((cols[0], cols[1].split(), cols[1].split()))
            return lines

    def _create_examples(self, lines, set_type):
        examples = []
        for (i, src, trg) in lines:
            guid = "%s-%s" % (set_type, i)
            if len(src) == len(trg):
                examples.append(InputExample(guid=guid, src=src, trg=trg))
        return examples


class DataProcessorForTagging(DataProcessor):
    @staticmethod
    def convert_examples_to_features(examples, max_seq_length, tokenizer, verbose=True):
        features = []
        for i, example in enumerate(examples):
            encoded_inputs = tokenizer(example.src,
                                       max_length=max_seq_length,
                                       padding="max_length",
                                       truncation=True,
                                       is_split_into_words=True)
            src_ids = encoded_inputs["input_ids"]
            attention_mask = encoded_inputs["attention_mask"]
            trg_ids = tokenizer(example.trg,
                                max_length=max_seq_length,
                                padding="max_length",
                                truncation=True,
                                is_split_into_words=True)["input_ids"]

            assert len(src_ids) == max_seq_length
            assert len(attention_mask) == max_seq_length
            assert len(trg_ids) == max_seq_length

            if verbose and i < 5:
                logger.info("*** Example ***")
                logger.info("guid: %s" % example.guid)
                logger.info("src_tokens: %s" % " ".join(example.src))
                logger.info("trg_tokens: %s" % " ".join(example.trg))
                logger.info("src_ids: %s" % " ".join([str(x) for x in src_ids]))
                logger.info("trg_ids: %s" % " ".join([str(x) for x in trg_ids]))
                logger.info("attention_mask: %s" % " ".join([str(x) for x in attention_mask]))

            features.append(
                    InputFeatures(src_ids=src_ids,
                                  attention_mask=attention_mask,
                                  trg_ids=trg_ids)
            )
        return features


class DataProcessorForRephrasing(DataProcessor):
    @staticmethod
    def convert_examples_to_features(examples, max_seq_length, tokenizer, verbose=True, skip_unk=False):
        features = []
        for i, example in enumerate(examples):
            src_ids = tokenizer(example.src,
                                max_length=max_seq_length // 2 - 2,
                                truncation=True,
                                is_split_into_words=True,
                                add_special_tokens=False).input_ids
            trg_ids = tokenizer(example.trg,
                                max_length=max_seq_length // 2 - 2,
                                truncation=True,
                                is_split_into_words=True,
                                add_special_tokens=False).input_ids
            src_ids = [_ for _ in src_ids if _ != 6]
            trg_ids = [_ for _ in trg_ids if _ != 6]
            if skip_unk:
                if (tokenizer.unk_token_id in src_ids) or (tokenizer.unk_token_id in trg_ids):
                    continue
            input_ids = [tokenizer.cls_token_id] + src_ids + [tokenizer.sep_token_id] + [tokenizer.mask_token_id for _ in trg_ids] + [tokenizer.sep_token_id]
            label_ids = [tokenizer.cls_token_id] + src_ids + [tokenizer.sep_token_id] + trg_ids + [tokenizer.sep_token_id]
            ref_ids = [tokenizer.cls_token_id] + trg_ids + [tokenizer.sep_token_id] + trg_ids + [tokenizer.sep_token_id]
            attention_mask = [1] * len(input_ids)
            if len(src_ids) != len(trg_ids):
                continue

            offset_length = max_seq_length - len(input_ids)
            if offset_length > 0:
                input_ids = input_ids + [tokenizer.pad_token_id] * offset_length
                attention_mask = attention_mask + [0] * offset_length
                label_ids = label_ids + [tokenizer.pad_token_id] * offset_length
                ref_ids = ref_ids + [tokenizer.pad_token_id] * offset_length
            input_ids, attention_mask, label_ids, ref_ids = input_ids[:max_seq_length], attention_mask[:max_seq_length], label_ids[:max_seq_length], ref_ids[:max_seq_length]

            assert len(input_ids) == max_seq_length
            assert len(attention_mask) == max_seq_length
            assert len(label_ids) == max_seq_length

            if verbose and i < 5:
                logger.info("*** Example ***")
                logger.info("guid: %s" % example.guid)
                logger.info("src_tokens: %s" % " ".join(tokenizer.convert_ids_to_tokens(input_ids)))
                logger.info("trg_tokens: %s" % " ".join(tokenizer.convert_ids_to_tokens(label_ids)))
                logger.info("src_ids: %s" % " ".join([str(x) for x in input_ids]))
                logger.info("trg_ids: %s" % " ".join([str(x) for x in label_ids]))
                logger.info("attention_mask: %s" % " ".join([str(x) for x in attention_mask]))

            features.append(
                    InputFeatures(src_ids=input_ids,
                                  attention_mask=attention_mask,
                                  trg_ids=label_ids,
                                  trg_ref_ids=ref_ids)
            )
        return features


class DataProcessorForTaggingConfus(DataProcessor):
    def load_confus(self, confus_dir="../GPT-CSC/confus"):
        with open(os.path.join(confus_dir, "stroke.json"), "r") as f:
            self.stroke_confus = json.load(f)
        with open(os.path.join(confus_dir, "pinyin_sim.json"), "r") as f:
            self.pinyin_sim_confus = json.load(f)
        with open(os.path.join(confus_dir, "pinyin_sam.json"), "r") as f:
            self.pinyin_sam_confus = json.load(f)
        with open(os.path.join(confus_dir, "word_freq.txt"), "r") as f:
            self.word_list = [line.strip() for line in f.readlines()]
    
    @staticmethod
    def ishan(char):
        return "\u4e00" <= char <= "\u9fff"
    
    def confus_for_src(self, src, trg):
        tokens = deepcopy(src)
        trg_tokens = trg
        j = -1
        for i, (s, t) in enumerate(zip(tokens, trg_tokens)):
            if s != t:
                j = i
                break
        all_indices = list(range(max(0, i - 5), min(len(tokens), i + 5 + 1)))
        choosen = random.choice(all_indices)
        if choosen == j or not self.ishan(tokens[choosen]):
            return tokens
        j = choosen
        rn = random.random()
        if rn < 0.4:
            if tokens[j] in self.pinyin_sam_confus:
                tokens[j] = random.choice(self.pinyin_sam_confus[tokens[j]])
            else:
                tokens[j] = random.choice(self.word_list)
        elif rn < 0.7:
            if tokens[j] in self.pinyin_sim_confus:
                tokens[j] = random.choice(self.pinyin_sim_confus[tokens[j]])
            else:
                tokens[j] = random.choice(self.word_list)
        elif rn < 0.9:
            if tokens[j] in self.stroke_confus:
                tokens[j] = random.choice(self.stroke_confus[tokens[j]])
            else:
                tokens[j] = random.choice(self.word_list)
        else:
            tokens[j] = random.choice(self.word_list)

        return tokens

    def convert_examples_to_features(self, examples, max_seq_length, tokenizer, verbose=True):
        self.load_confus()

        features = []
        for i, example in enumerate(examples):
            encoded_inputs = tokenizer(example.src,
                                       max_length=max_seq_length,
                                       padding="max_length",
                                       truncation=True,
                                       is_split_into_words=True)
            src_ids = encoded_inputs["input_ids"]
            attention_mask = encoded_inputs["attention_mask"]
            trg_ids = tokenizer(example.trg,
                                max_length=max_seq_length,
                                padding="max_length",
                                truncation=True,
                                is_split_into_words=True)["input_ids"]

            noisy_src = self.confus_for_src(example.src, example.trg)
            noisy_src_ids = tokenizer(noisy_src,
                                      max_length=max_seq_length,
                                      padding="max_length",
                                      truncation=True,
                                      is_split_into_words=True)["input_ids"]

            assert len(src_ids) == max_seq_length
            assert len(attention_mask) == max_seq_length
            assert len(trg_ids) == max_seq_length
            assert len(noisy_src_ids) == max_seq_length

            if verbose and i < 5:
                logger.info("*** Example ***")
                logger.info("guid: %s" % example.guid)
                logger.info("src_tokens:\t%s" % " ".join(example.src))
                logger.info("noisy_tokens:\t%s" % " ".join(noisy_src))
                logger.info("trg_tokens:\t%s" % " ".join(example.trg))
                logger.info("src_ids: %s" % " ".join([str(x) for x in src_ids]))
                logger.info("trg_ids: %s" % " ".join([str(x) for x in trg_ids]))
                logger.info("attention_mask: %s" % " ".join([str(x) for x in attention_mask]))

            features.append(
                    InputFeatures(src_ids=src_ids,
                                  attention_mask=attention_mask,
                                  trg_ids=trg_ids,
                                  trg_ref_ids=noisy_src_ids)
            )
        return features


class Metrics:

    @staticmethod
    def compute_detect(src_sents, trg_sents, prd_sents):

        pos_sents, neg_sents, tp_sents, fp_sents, fn_sents, prd_pos_sents, prd_neg_sents = 0, 0, 0, 0, 0, 0, 0
        for s, t, p in zip(src_sents, trg_sents, prd_sents):
            if s != t:
                pos_sents+=1
                if p != s:
                    tp_sents+=1
                else:
                    fn_sents+=1

            else:
                neg_sents+=1
                if p != t:
                    fp_sents+=1

            if s != p:
                prd_pos_sents+=1
            else:
                prd_neg_sents+=1

        p = 1.0 * tp_sents / (prd_pos_sents+ 1e-12)
        r = 1.0 * tp_sents / (pos_sents+ 1e-12)
        f1 = 2.0 * (p * r) / (p + r + 1e-12)
        fpr = 1.0 * fp_sents / (neg_sents + 1e-12)

        return p, r, f1, fpr
    
    @staticmethod
    def compute(src_sents, trg_sents, prd_sents):
        def difference(src, trg):
            ret = copy.deepcopy(src)
            for i, (src_char, trg_char) in enumerate(zip(src, trg)):
                if src_char!= trg_char:
                    ret[i] = "(" + src_char + "->" + trg_char + ")"

            return "".join(ret)

        pos_sents, neg_sents, tp_sents, fp_sents, fn_sents, prd_pos_sents, prd_neg_sents = [], [], [], [], [], [], []
        for s, t, p in zip(src_sents, trg_sents, prd_sents):
            if s != t:
                pos_sents.append(difference(s, t))
                if p == t:
                    tp_sents.append(difference(s, t))
                if p == s:
                    fn_sents.append(difference(s, t))

            else:
                neg_sents.append(difference(s, t))
                if p != t:
                    fp_sents.append(difference(t, p))

            if s != p:
                prd_pos_sents.append(difference(s, p))
            if s == p:
                prd_neg_sents.append(difference(s, p))

        p = 1.0 * len(tp_sents) / (len(prd_pos_sents)+ 1e-12)
        r = 1.0 * len(tp_sents) / (len(pos_sents)+ 1e-12)
        f1 = 2.0 * (p * r) / (p + r + 1e-12)
        fpr = 1.0 * len(fp_sents) / (len(neg_sents) + 1e-12)

        return p, r, f1, fpr, tp_sents, fp_sents, fn_sents

def equals(src,trg):
    if len(src)!=len(trg):
        return False
    for i,(st,tt) in enumerate(zip(src,trg)):
        # we do not consider the punctuation
        if st not in ['.','。',',','，','?','？',':','：','!','！'] and st!=tt:
            return False
    return True

def mask_tokens(inputs, tokenizer, noise_probability=0.2):
    inputs = inputs.clone()
    probability_matrix = torch.full(inputs.shape, noise_probability)
    special_tokens_mask = [
        tokenizer.get_special_tokens_mask(val, already_has_special_tokens=True) for val in inputs.tolist()
    ]
    special_tokens_mask = torch.tensor(special_tokens_mask, dtype=torch.bool)

    probability_matrix.masked_fill_(special_tokens_mask, value=0.0)
    masked_indices = torch.bernoulli(probability_matrix).bool()
    inputs[masked_indices] = tokenizer.convert_tokens_to_ids(tokenizer.mask_token)

    return inputs


def mask_tokens_only_neg(inputs, labels, tokenizer, noise_probability=0.2):
    inputs = inputs.clone()
    probability_matrix = torch.full(inputs.shape, noise_probability)
    special_tokens_mask = [
        tokenizer.get_special_tokens_mask(val, already_has_special_tokens=True) for val in inputs.tolist()
    ]
    special_tokens_mask = torch.tensor(special_tokens_mask, dtype=torch.bool)
    neq_tokens_mask = (inputs != labels).cpu()

    probability_matrix.masked_fill_(special_tokens_mask + neq_tokens_mask, value=0.0)
    masked_indices = torch.bernoulli(probability_matrix).bool()
    inputs[masked_indices] = tokenizer.convert_tokens_to_ids(tokenizer.mask_token)

    return inputs


def mask_tokens_only_neg_2(inputs, noisy_inputs, labels, tokenizer, noise_probability=0.2):
    inputs = inputs.clone()
    noisy_inputs = noisy_inputs.clone()
    labels = labels.clone()
    probability_matrix = torch.full(inputs.shape, noise_probability)
    special_tokens_mask = [
        tokenizer.get_special_tokens_mask(val, already_has_special_tokens=True) for val in inputs.tolist()
    ]
    special_tokens_mask = torch.tensor(special_tokens_mask, dtype=torch.bool)
    neq_tokens_mask = (inputs != labels).cpu()

    probability_matrix.masked_fill_(special_tokens_mask + neq_tokens_mask, value=0.0)
    masked_indices = torch.bernoulli(probability_matrix).bool()
    inputs[masked_indices] = tokenizer.convert_tokens_to_ids(tokenizer.mask_token)
    noisy_inputs[masked_indices] = tokenizer.convert_tokens_to_ids(tokenizer.mask_token)

    return inputs, noisy_inputs


def main():
    parser = argparse.ArgumentParser()

    # Data config
    parser.add_argument("--data_dir", type=str, default="data/",
                        help="Directory to contain the input data for all tasks.")
    parser.add_argument("--train_on", type=str, default="mak/train_data.tsv",
                        help="Specify a training set.")
    parser.add_argument("--eval_on", type=str, default="mak/dev_data.tsv",
                        help="Specify a dev set.")
    parser.add_argument("--test_on", type=str, default="mak/test_data.tsv",
                        help="Specify a test set.")
    parser.add_argument("--load_model_path", type=str, default="bert-base-chinese",
                        help="Pre-trained model path to load.")
    parser.add_argument("--model_type", type=str, default="relm",
                        help="Model architecture to load.")
    parser.add_argument("--cache_dir", type=str, default="cache/",
                        help="Directory to store the pre-trained language models downloaded from s3.")
    parser.add_argument("--output_dir", type=str, default="output/",
                        help="Directory to output predictions and checkpoints.")
    parser.add_argument("--load_state_dict", type=str, default="",
                        help="Trained model weights to load for evaluation if needed.")

    # Training config
    parser.add_argument("--do_train", action="store_true",
                        help="Whether to run training.")
    parser.add_argument("--do_eval", action="store_true",
                        help="Whether to evaluate on the dev set.")
    parser.add_argument("--do_test", action="store_true",
                        help="Whether to test on the test set.")
    parser.add_argument("--use_slow_tokenizer", action="store_true",
                        help="A slow tokenizer will be used if passed.")
    parser.add_argument("--do_lower_case", action="store_true",
                        help="Set this flag if you are using an uncased model.")
    parser.add_argument("--max_seq_length", type=int, default=128,
                        help="Maximum total input sequence length after word-piece tokenization.")
    parser.add_argument("--train_batch_size", type=int, default=128,
                        help="Total batch size for training.")
    parser.add_argument("--eval_batch_size", type=int, default=128,
                        help="Total batch size for evaluation.")
    parser.add_argument("--test_batch_size", type=int, default=128,
                        help="Total batch size for testing.")
    parser.add_argument("--learning_rate", type=float, default=1e-5,
                        help="Initial learning rate for Adam.")
    parser.add_argument("--num_train_epochs", type=float, default=1000.0,
                        help="Total number of training epochs to perform.")
    parser.add_argument("--max_train_steps", type=int, default=1000,
                        help="Total number of training steps to perform. If provided, overrides training epochs.")
    parser.add_argument("--lr_scheduler_type", type=str, default="constant",
                        help="Scheduler type for learning rate warmup.")
    parser.add_argument("--warmup_proportion", type=float, default=0.06,
                        help="Proportion of training to perform learning rate warmup for.")
    parser.add_argument("--weight_decay", type=float, default=0.,
                        help="L2 weight decay for training.")
    parser.add_argument("--gradient_accumulation_steps", type=int, default=1,
                        help="Number of updates steps to accumulate before performing a backward pass.")
    parser.add_argument("--no_cuda", action="store_true",
                        help="Whether not to use CUDA when available.")
    parser.add_argument("--fp16", action="store_true",
                        help="Whether to use mixed precision.")
    parser.add_argument("--seed", type=int, default=1024,
                        help="Random seed for initialization.")
    parser.add_argument("--save_steps", type=int, default=100,
                        help="How many steps to save the checkpoint once.")
    parser.add_argument("--noise_probability", type=float, default=0.2,
                        help="Mask rate for masked-fine-tuning.")
    parser.add_argument("--mft", action="store_true",
                        help="Training with masked-fine-tuning.")
    parser.add_argument("--detect", action="store_true", )

    parser.add_argument("--response_file", type=str)
    parser.add_argument("--eval_mode", type=str, default="predict",
                        help="evaluate or predict, for predict mode there is no true label. (The pseudo label is the same with the input)")

    args = parser.parse_args()

    relm = args.model_type.startswith("relm")
    craspell = args.model_type.startswith("craspell")

    AutoCSC = {
        "finetune": AutoCSCfinetune,
        "softmasked": AutoCSCSoftMasked,
        "mdcspell": AutoCSCMDCSpell,
        "craspell": AutoCSCCRASpell,
        "relm": AutoCSCReLM,
        "relm-prompt": AutoCSCReLMPrompt,
    }

    if relm:
        processor = DataProcessorForRephrasing()
    elif craspell:
        processor = DataProcessorForTaggingConfus()
    else:
        processor = DataProcessorForTagging()

    device = torch.device("cuda" if torch.cuda.is_available() and not args.no_cuda else "cpu")
    n_gpu = torch.cuda.device_count()

    args.train_batch_size = args.train_batch_size // args.gradient_accumulation_steps

    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    if n_gpu > 0:
        torch.cuda.manual_seed_all(args.seed)

    if not os.path.exists(args.output_dir):
        os.makedirs(args.output_dir)


    @functools.lru_cache(maxsize=None)
    def _cached_log_stream(filename):
        io = open(filename, "a", buffering=1024)
        atexit.register(io.close)
        return io

    fh = logging.StreamHandler(_cached_log_stream(os.path.join(args.output_dir, "std_out.log")))
    fh.setLevel(logging.DEBUG)
    logger.addHandler(fh)

    logger.info(args)
    logger.info("device: {}, n_gpu: {}, distributed training: {}, 16-bits training: {}".format(
        device, n_gpu, "-accelerate", args.fp16))

    if args.do_train:
        torch.save(args, os.path.join(args.output_dir, "train_args.bin"))

    cache_dir = args.cache_dir
    tokenizer = AutoTokenizer.from_pretrained(args.load_model_path,
                                              do_lower_case=args.do_lower_case,
                                              cache_dir=cache_dir,
                                              use_fast=not args.use_slow_tokenizer,
                                              add_prefix_space=True)

    if args.do_train:
        accelerator = Accelerator(cpu=args.no_cuda, mixed_precision="fp16" if args.fp16 else "no")
        device = accelerator.device

        train_examples = processor.get_train_examples(args.data_dir, args.train_on)
        train_features = processor.convert_examples_to_features(train_examples, args.max_seq_length, tokenizer)

        all_input_ids = torch.LongTensor([f.src_ids for f in train_features])
        all_input_mask = torch.LongTensor([f.attention_mask for f in train_features])
        all_label_ids = torch.LongTensor([f.trg_ids for f in train_features])
        if relm or craspell:
            all_ref_ids = torch.LongTensor([f.trg_ref_ids for f in train_features])
            train_data = TensorDataset(all_input_ids, all_input_mask, all_label_ids, all_ref_ids)
        else:
            train_data = TensorDataset(all_input_ids, all_input_mask, all_label_ids)

        train_dataloader = DataLoader(train_data, shuffle=True, batch_size=args.train_batch_size)
        train_dataloader = accelerator.prepare(train_dataloader)

        num_update_steps_per_epoch = math.ceil(len(train_dataloader) / args.gradient_accumulation_steps)
        if args.max_train_steps is None:
            args.max_train_steps = int(args.num_train_epochs * num_update_steps_per_epoch)
        else:
            args.num_train_epochs = math.ceil(args.max_train_steps / num_update_steps_per_epoch)

        model = AutoCSC[args.model_type].from_pretrained(args.load_model_path,
                                                         cache_dir=cache_dir,
                                                         state_dict=torch.load(
                                                             args.load_state_dict, map_location=device) if args.load_state_dict else None)

        no_decay = ["bias", "LayerNorm.bias", "LayerNorm.weight"]
        optimizer_grouped_parameters = [
            {
                "params": [p for n, p in model.named_parameters() if not any(nd in n for nd in no_decay)],
                "weight_decay": args.weight_decay
            },
            {
                "params": [p for n, p in model.named_parameters() if any(nd in n for nd in no_decay)],
                "weight_decay": 0.0
            }
        ]

        optimizer = torch.optim.AdamW(optimizer_grouped_parameters, lr=args.learning_rate)
        scheduler = get_scheduler(name=args.lr_scheduler_type,
                                  optimizer=optimizer,
                                  num_warmup_steps=args.max_train_steps * args.warmup_proportion,
                                  num_training_steps=args.max_train_steps)

        model, optimizer, scheduler = accelerator.prepare(model, optimizer, scheduler)

        if args.do_eval:
            eval_examples = processor.get_dev_examples(args.data_dir, args.eval_on)
            test_features = processor.convert_examples_to_features(eval_examples, args.max_seq_length, tokenizer)

            all_input_ids = torch.LongTensor([f.src_ids for f in test_features])
            all_input_mask = torch.LongTensor([f.attention_mask for f in test_features])
            all_label_ids = torch.LongTensor([f.trg_ids for f in test_features])
            if relm or craspell:
                all_ref_ids = torch.LongTensor([f.trg_ref_ids for f in test_features])
                test_data = TensorDataset(all_input_ids, all_input_mask, all_label_ids, all_ref_ids)
            else:
                test_data = TensorDataset(all_input_ids, all_input_mask, all_label_ids)

            test_dataloader = DataLoader(test_data, shuffle=False, batch_size=args.eval_batch_size)
            test_dataloader = accelerator.prepare(test_dataloader)

    if args.do_train:
        logger.info("***** Running training *****")
        logger.info("  Num examples = %d", len(train_features))
        logger.info("  Batch size = %d", args.train_batch_size * accelerator.num_processes)
        logger.info("  Num steps = %d", args.max_train_steps)

        tbwriter = SummaryWriter(log_dir=os.path.join(args.output_dir, "tfevents"))

        progress_bar = tqdm(range(args.max_train_steps), desc="Iteration")
        global_step = 0
        best_result = list()
        wrap = False
        for epoch in range(int(args.num_train_epochs)):
            if wrap: break
            train_loss = 0
            num_train_examples = 0
            train_steps = 0

            for step, batch in enumerate(train_dataloader):
                model.train()
                batch = tuple(t.to(device) for t in batch)
                if relm:
                    src_ids, attention_mask, trg_ids, trg_ref_ids = batch
                    src_ids = mask_tokens_only_neg(src_ids, trg_ref_ids, tokenizer, args.noise_probability)
                elif craspell:
                    src_ids, attention_mask, trg_ids, noisy_src_ids = batch
                    if args.mft:
                        src_ids, noisy_src_ids = mask_tokens_only_neg_2(src_ids, noisy_src_ids, trg_ids, tokenizer,
                                                                        args.noise_probability)
                else:
                    src_ids, attention_mask, trg_ids = batch
                    if args.mft:
                        src_ids = mask_tokens_only_neg(src_ids, trg_ids, tokenizer, args.noise_probability)

                if not craspell:
                    outputs = model(src_ids=src_ids,
                                    attention_mask=attention_mask,
                                    trg_ids=trg_ids)
                else:
                    outputs = model(src_ids=src_ids,
                                    attention_mask=attention_mask,
                                    trg_ids=trg_ids,
                                    noisy_src_ids=noisy_src_ids)
                loss = outputs["loss"]

                if n_gpu > 1:
                    loss = loss.mean()
                if args.gradient_accumulation_steps > 1:
                    loss = loss / args.gradient_accumulation_steps
                accelerator.backward(loss)

                train_loss += loss.item()
                num_train_examples += src_ids.size(0)
                train_steps += 1
                if (step + 1) % args.gradient_accumulation_steps == 0 or step == len(train_dataloader) - 1:
                    optimizer.step()
                    optimizer.zero_grad()
                    scheduler.step()
                    global_step += 1
                    progress_bar.update(1)

                if args.do_eval and global_step % args.save_steps == 0:
                    logger.info("***** Running evaluation *****")
                    logger.info("  Num examples = %d", len(test_features))
                    logger.info("  Batch size = %d", args.eval_batch_size * accelerator.num_processes)

                    def decode(input_ids):
                        return tokenizer.convert_ids_to_tokens(input_ids, skip_special_tokens=True)

                    model.eval()
                    all_inputs, all_labels, all_predictions = [], [], []
                    for batch in tqdm(test_dataloader):
                        batch = tuple(t.to(device) for t in batch)
                        src_ids, attention_mask, trg_ids = batch[:3]
                        with torch.no_grad():
                            outputs = model(src_ids=src_ids,
                                            attention_mask=attention_mask,
                                            trg_ids=trg_ids)
                            prd_ids = outputs["predict_ids"]

                        src_ids, trg_ids, prd_ids = accelerator.gather_for_metrics((src_ids, trg_ids, prd_ids))
                        for s, t, p in zip(src_ids.tolist(), trg_ids.tolist(), prd_ids.tolist()):
                            if relm:
                                _t = [tt for tt, st in zip(t, s) if st == tokenizer.mask_token_id]
                                _p = [pt for pt, st in zip(p, s) if st == tokenizer.mask_token_id]

                                all_inputs += [decode(s)]
                                all_labels += [decode(_t)]
                                all_predictions += [decode(_p)]

                            else:
                                all_inputs += [decode(s)]
                                all_labels += [decode(t)]
                                all_predictions += [decode(p)]

                    loss = train_loss / train_steps
                    p, r, f1, fpr, tp, fp, fn = Metrics.compute(all_inputs, all_labels, all_predictions)

                    output_tp_file = os.path.join(args.output_dir, "sents.tp")
                    with open(output_tp_file, "w") as writer:
                        for line in tp:
                            writer.write(line + "\n")
                    output_fp_file = os.path.join(args.output_dir, "sents.fp")
                    with open(output_fp_file, "w") as writer:
                        for line in fp:
                            writer.write(line + "\n")
                    output_fn_file = os.path.join(args.output_dir, "sents.fn")
                    with open(output_fn_file, "w") as writer:
                        for line in fn:
                            writer.write(line + "\n")

                    result = {
                        "global_step": global_step,
                        "loss": loss,
                        "eval_p": p * 100,
                        "eval_r": r * 100,
                        "eval_f1": f1 * 100,
                        "eval_fpr": fpr * 100,
                    }
                    if accelerator.is_local_main_process:
                        model_to_save = model.module if hasattr(model, "module") else model
                        output_model_file = os.path.join(args.output_dir,
                                                         "step-%s_f1-%.2f.bin" % (str(global_step), result["eval_f1"]))
                        torch.save(model_to_save.state_dict(), output_model_file)
                        best_result.append((result["eval_f1"], output_model_file))
                        best_result.sort(key=lambda x: x[0], reverse=True)
                        if len(best_result) > 3:
                            _, model_to_remove = best_result.pop()
                            os.remove(model_to_remove)

                        output_eval_file = os.path.join(args.output_dir, "eval_results.txt")
                        with open(output_eval_file, "a") as writer:
                            logger.info("***** Eval results *****")
                            writer.write(
                                "Global step = %s | loss = %.3f | eval precision = %.2f | eval recall = %.2f | eval f1 = %.2f | eval fpr = %.2f\n"
                                % (str(result["global_step"]),
                                   result["loss"],
                                   result["eval_p"],
                                   result["eval_r"],
                                   result["eval_f1"],
                                   result["eval_fpr"]))
                            for k, v in result.items():
                                if (k == "global_step"): continue
                                tbwriter.add_scalar(k, v, result["global_step"])
                            for key in sorted(result.keys()):
                                logger.info("Global step: %s,  %s = %s", str(global_step), key, str(result[key]))
                else:
                    loss = train_loss / train_steps
                    tbwriter.add_scalar("loss", loss, global_step)
                    logger.info("Global step = %s | loss = %.3f" % (global_step, loss))
                if global_step >= args.max_train_steps:
                    wrap = True
                    break

    if args.do_test:
        accelerator = Accelerator(cpu=args.no_cuda, mixed_precision="fp16" if args.fp16 else "no")
        device = accelerator.device

        model = AutoCSC[args.model_type].from_pretrained(args.load_model_path,
                                                         state_dict=torch.load(args.load_state_dict, map_location=device),
                                                         cache_dir=cache_dir)
        model = accelerator.prepare(model)

        test_examples = processor.get_test_examples(args.data_dir, args.test_on)
        logger.info("test on: {}".format(args.test_on))
        test_features = processor.convert_examples_to_features(test_examples, args.max_seq_length, tokenizer, False)

        all_input_ids = torch.tensor([f.src_ids for f in test_features], dtype=torch.long)
        all_input_mask = torch.tensor([f.attention_mask for f in test_features], dtype=torch.long)
        all_label_ids = torch.tensor([f.trg_ids for f in test_features], dtype=torch.long)
        if relm:
            all_ref_ids = torch.LongTensor([f.trg_ref_ids for f in test_features])
            test_data = TensorDataset(all_input_ids, all_input_mask, all_label_ids, all_ref_ids)
        else:
            test_data = TensorDataset(all_input_ids, all_input_mask, all_label_ids)

        test_dataloader = DataLoader(test_data, shuffle=False, batch_size=args.test_batch_size)
        test_dataloader = accelerator.prepare(test_dataloader)

        def decode(input_ids):
            return tokenizer.convert_ids_to_tokens(input_ids, skip_special_tokens=True)

        model.eval()
        all_inputs, all_labels, all_predictions = [], [], []
        for batch in tqdm(test_dataloader, leave=False):
            batch = tuple(t.to(device) for t in batch)
            src_ids, attention_mask, trg_ids = batch[:3]
            with torch.no_grad():
                outputs = model(src_ids=src_ids,
                                attention_mask=attention_mask,
                                trg_ids=trg_ids)
                prd_ids = outputs["predict_ids"]

            src_ids, trg_ids, prd_ids = accelerator.gather_for_metrics((src_ids, trg_ids, prd_ids))
            for s, t, p in zip(src_ids.tolist(), trg_ids.tolist(), prd_ids.tolist()):
                if relm:
                    _t = [tt for tt, st in zip(t, s) if st == tokenizer.mask_token_id]
                    _p = [pt for pt, st in zip(p, s) if st == tokenizer.mask_token_id]

                    all_inputs += [decode(s)]
                    all_labels += [decode(_t)]
                    all_predictions += [decode(_p)]

                else:
                    all_inputs += [decode(s)]
                    all_labels += [decode(t)]
                    all_predictions += [decode(p)]

        if args.eval_mode == "evaluate":

            if args.detect:
                p, r, f1, fpr = Metrics.compute_detect(all_inputs, all_labels, all_predictions)
                logger.info("detect: p = %.2f, r = %.2f, f1 = %.2f, fpr = %.2f \n", p * 100, r * 100, f1 * 100,
                            fpr * 100)

            p, r, f1, fpr, tp, fp, fn = Metrics.compute(all_inputs, all_labels, all_predictions)

            if args.response_file:
                output_file = os.path.join(args.output_dir, args.response_file)
                with open(output_file, "w") as writer:
                    for input, label, prediction in zip(all_inputs, all_labels, all_predictions):
                        writer.write("input: " + " ".join(input) + "\t")
                        writer.write("label: " + " ".join(label) + "\t")
                        writer.write("prediction: " + " ".join(prediction) + "\t")
                        if prediction == label:
                            writer.write("correct\n")
                        else:
                            writer.write("wrong\n")

            result = {
                "eval_p": p * 100,
                "eval_r": r * 100,
                "eval_f1": f1 * 100,
                "eval_fpr": fpr * 100,
            }
            output_eval_file = os.path.join(args.output_dir, "eval_results.txt")
            with open(output_eval_file, "a") as writer:
                logger.info("***** Eval results *****")
                writer.write("eval {} on {}. \n".format(args.model_type, args.eval_on))
                writer.write("use checkpoint: {}\n".format(args.load_state_dict))
                for key in sorted(result.keys()):
                    writer.write("%s = %s\n" % (key, str(result[key])))
                    logger.info("Global step: %s,  %s = %s", str(-1), key, str(result[key]))
        else:
            assert args.eval_mode == "predict"
            output_file = os.path.join(args.output_dir, "submission_{}_{}.csv".format(args.model_type, int(time.time())))
            with open(output_file, "w") as writer:
                writer.write("ID,Output\n")
                for i, prediction in enumerate(all_predictions):
                    writer.write('{},"{}"\n'.format(i, "".join(prediction)))
            logger.info("Predictions saved to {}".format(output_file))


if __name__ == "__main__":
    main()
