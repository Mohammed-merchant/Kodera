import torch
from datasets import load_dataset
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    BitsAndBytesConfig
)
from peft import LoraConfig, prepare_model_for_kbit_training
from trl import SFTTrainer, SFTConfig
MODEL_NAME = "Qwen/Qwen2.5-1.5B-Instruct"
DATASET_PATH = "train_data/final_dataset.jsonl"
OUTPUT_DIR = "kodeara_adapter_v2"
if not torch.cuda.is_available():
    raise RuntimeError("CUDA is not available. Please ensure you have a compatible GPU and the necessary drivers installed.")
print(f'GPU is {torch.cuda.get_device_name(torch.cuda.current_device())}')
print("VRAM:", round(
    torch.cuda.get_device_properties(0).total_memory / 1024**3, 2
), "GB")
torch.cuda.empty_cache()
tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
if tokenizer.pad_token is None:
    tokenizer.pad_token = tokenizer.eos_token
bnb_config = BitsAndBytesConfig(
    load_in_4bit=True,
    bnb_4bit_quant_type="nf4",
    bnb_4bit_use_double_quant=True,
    bnb_4bit_compute_dtype=torch.float16
)
print("\nLoading base model...")

model = AutoModelForCausalLM.from_pretrained(
    MODEL_NAME,
    quantization_config=bnb_config,
    device_map="auto",
    torch_dtype=torch.float16
)
model.config.use_cache = False
model = prepare_model_for_kbit_training(model)

# Load dataset
print("\nLoading dataset...")

dataset = load_dataset(
    "json",
    data_files=DATASET_PATH,
    split="train"
)
print("Training samples:", len(dataset))

# LoRA configuration
lora_config = LoraConfig(
    r=8,
    lora_alpha=32,
    lora_dropout=0.05,
    target_modules=[
        "q_proj",
        "k_proj",
        "v_proj",
        "o_proj"
    ],
    task_type="CAUSAL_LM"
)
print("\nLoRA configuration:", lora_config)
training_config = SFTConfig(
    output_dir=OUTPUT_DIR,

    num_train_epochs=1,

    per_device_train_batch_size=1,
    gradient_accumulation_steps=8,

    gradient_checkpointing=True,
    gradient_checkpointing_kwargs={
        "use_reentrant": False
    },

    learning_rate=1e-4,

    max_length=256,

    fp16=True,
    bf16=False,

    logging_steps=5,

    save_strategy="epoch",
    save_total_limit=1,

    report_to="none",
    optim="adamw_torch"
)
print(f'the training args done now starting ======')
print(f'training args: {training_config}')
trainer = SFTTrainer(
    model=model,
    args=training_config,
    train_dataset=dataset,
    peft_config=lora_config,
    processing_class=tokenizer
)
for name, param in trainer.model.named_parameters():
    if param.requires_grad:
        print(f"Trainable parameter: {name}")
        print(f"Shape: {param.shape}")
        print(f"Data type before: {param.dtype}")

        param.data = param.data.float()

        print(f"Data type after: {param.dtype}")
        print("-" * 40)


print(f'the trainer is created now starting training ======')
trainer.train()
print(f'the training is done now saving the model ======')
trainer.model.save_pretrained(OUTPUT_DIR)
tokenizer.save_pretrained(OUTPUT_DIR)
print("\nTraining completed!")
print("Adapter saved in:", OUTPUT_DIR)