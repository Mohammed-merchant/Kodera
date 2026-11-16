
import torch
from datasets import load_dataset
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
from peft import LoraConfig, prepare_model_for_kbit_training
from trl import SFTTrainer, SFTConfig

MODEL = "Qwen/Qwen2.5-1.5B-Instruct"
OUTPUT_DIR = "kodeara_adapter_v2"

bnb = BitsAndBytesConfig(
    load_in_4bit=True,
    bnb_4bit_quant_type="nf4",
    bnb_4bit_use_double_quant=True,
    bnb_4bit_compute_dtype=torch.bfloat16
)

tok = AutoTokenizer.from_pretrained(MODEL)
model = AutoModelForCausalLM.from_pretrained(
    MODEL,
    quantization_config=bnb,
    device_map="auto"
)

model.config.use_cache = False
model = prepare_model_for_kbit_training(model)

data = load_dataset(
    "json",
    data_files="train_data/final_dataset.jsonl",
    split="train"
)

lora = LoraConfig(
    r=8,
    lora_alpha=32,
    lora_dropout=0.05,
    target_modules=["q_proj", "k_proj", "v_proj", "o_proj"],
    task_type="CAUSAL_LM"
)

cfg = SFTConfig(
    output_dir=OUTPUT_DIR,
    num_train_epochs=1,
    per_device_train_batch_size=1,
    gradient_accumulation_steps=8,
    gradient_checkpointing=True,
    learning_rate=2e-4,
    max_length=256,
    bf16=True,
    logging_steps=1,
    save_strategy="epoch",
    report_to="none"
)

trainer = SFTTrainer(
    model=model,
    args=cfg,
    train_dataset=data,
    peft_config=lora,
    processing_class=tok
)

trainer.train()

trainer.model.save_pretrained(OUTPUT_DIR)
tok.save_pretrained(OUTPUT_DIR)

print("Fine-tuning completed. Adapter saved:", OUTPUT_DIR)
