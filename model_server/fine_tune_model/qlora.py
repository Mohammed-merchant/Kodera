import torch
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
MODEL = "Qwen/Qwen2.5-1.5B-Instruct"  # use whatever pull_qwen.py downloaded
bnb = BitsAndBytesConfig (
    
    load_in_4bit=True,
    bnb_4bit_quant_type="nf4",
    bnb_4bit_use_double_quant=True,
    bnb_4bit_compute_dtype=torch.bfloat16
 )
print(f'the model is loaded from {MODEL}')
tok = AutoTokenizer.from_pretrained(MODEL, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(
    MODEL,
    quantization_config=bnb,
    device_map="auto",
    trust_remote_code=True
)
print(f"Memory used: {model.get_memory_footprint() / 1e9:.2f} GB")