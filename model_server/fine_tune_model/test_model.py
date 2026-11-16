
import torch
from transformers import (
    AutoTokenizer,
    AutoModelForCausalLM,
    BitsAndBytesConfig
)
from peft import PeftModel

# 1. Model paths
model_name = "Qwen/Qwen2.5-1.5B-Instruct"
adapter_path = "kodeara_adapter_v2"

# 2. Load tokenizer
tok = AutoTokenizer.from_pretrained(model_name)

# 3. Configure 4-bit quantization
bnb_config = BitsAndBytesConfig(
    load_in_4bit=True,
    bnb_4bit_quant_type="nf4",
    bnb_4bit_compute_dtype=torch.float16,
    bnb_4bit_use_double_quant=True
)

# 4. Load base model
base_model = AutoModelForCausalLM.from_pretrained(
    model_name,
    quantization_config=bnb_config,
    device_map="auto"
)

# 5. Attach trained adapter
model = PeftModel.from_pretrained(
    base_model,
    adapter_path
)

model.eval()

print("Kodeara model loaded successfully!")

# 6. Take user input
while True:
    question = input("\nYou: ")

    if question.lower() in ["exit", "quit"]:
        break

    messages = [
        {
            "role": "system",
            "content": (
                "You are Kodeara, a friendly clinic "
                "administrative voice assistant. "
                "Speak naturally, politely, and briefly. "
                "Help with appointment requests and clinic "
                "information. Never invent business information. "
                "Do not diagnose or prescribe medicines."
            )
        },
        {
            "role": "user",
            "content": question
        }
    ]

    # 7. Convert messages into model input
    prompt = tok.apply_chat_template(
        messages,
        tokenize=False,
        add_generation_prompt=True
    )

    inputs = tok(
        prompt,
        return_tensors="pt"
    ).to(model.device)

    # 8. Generate response
    with torch.no_grad():
        outputs = model.generate(
            **inputs,
            max_new_tokens=120,
            do_sample=False,
            pad_token_id=tok.eos_token_id
        )

    # 9. Decode response
    response = tok.decode(
        outputs[0][inputs["input_ids"].shape[1]:],
        skip_special_tokens=True
    )

    print("Kodeara:", response)

print("Testing finished.")
