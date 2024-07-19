# ReLM for Chinese Spell Correction

The codebase is modified from [lemon](https://github.com/gingasan/lemon) to train, evaluate, and test on the MAK dataset.

> MAK Dataset may not be publicly available at the time. If you have the dataset, please place it in `data/mak` folder, and split the dataset by `train_data.tsv`, `dev_data.tsv` and `test_data.tsv`. And then using `python sft_csc_gen.py --input [file] --output [file]` to generate data in `data/mak_sft` folder in `train_data.jsonl`, `dev_data.jsonl` and `test_data.jsonl`.

## Usage

The repo is tested on Ubuntu 22.04, Nvidia A100 80G GPU.

1. Install Python and PyTorch with CUDA.
2. Install the dependencies.
```bash
pip install -r requirements.txt
```
3. Prepare [pre-trained model](https://drive.google.com/file/d/10vvkG_jzNK-CjIwlSvizhE1IOpnn9OqN/view?usp=share_link) (from the original [lemon](https://github.com/gingasan/lemon) repo) in `output/relm-m0.3.bin`. Before the next step, you could also set the HuggingFace mirror to speed up the base model downloading by
```bash
export HF_ENDPOINT=https://hf-mirror.com
```
4. Finetune ReLM (training)
```bash
python run.py --model_type relm \
    --do_train \
    --do_eval \
    --load_state_dict output/relm-m0.3.bin \
    --fp16 \
    --output_dir output/relm-1e-5
```
This will use the default hyperparameter for training: `--train_batch_size 128 --eval_batch_size 128 --learning_rate 1e-5 --max_train_steps 1000 --seed 1024`. You could reduce the batch size to reduce the vRAM usage.

In the other terminal, open tensorboard
```bash
tensorboard --logdir output --port=12333
```
5. Test ReLM for submission
```bash
python run.py --model_type relm \
    --do_test \
    --load_state_dict output/relm-1e-5/step-800_f1-92.20.bin \
    --output_dir output/relm-1e-5
```
The output csv will be in `output/relm-1e-5/submission_relm_xxxxx.csv`. The load_state_dict may vary a little bit on the F1 score.
