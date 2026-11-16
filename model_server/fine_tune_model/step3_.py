import torch
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
MODEL = "Qwen/Qwen2.5-1.5B-Instruct"
print(f'the model is loaded from {MODEL}')
bnb = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4",
                         bnb_4bit_use_double_quant=True, bnb_4bit_compute_dtype=torch.bfloat16)
print(f'quantization config: {bnb}')
tok = AutoTokenizer.from_pretrained(MODEL)
print(f'tokenizer is loaded from {MODEL}')
model = AutoModelForCausalLM.from_pretrained(MODEL, quantization_config=bnb, device_map="auto")
print(f"Memory used: {model.get_memory_footprint() / 1e9:.2f} GB")
SYS = "You are Kodeara, a friendly AI voice assistant. Speak naturally, politely, and briefly. Never invent business information."
tests = ["What is your name?", "What is the price of a consultation?",
         "I want to book an appointment.", "Tell me about the doctor's schedule.", "Why are you so slow?"]
for q in tests:
    messages =[{
        'role':'system','content':SYS
    },
    {
        'role':'user','content':q
    }
    ]
    text = tok.apply_chat_template(messages, tokenize=False, add_generation_prompt=True, enable_thinking=False)
    print(f'text is tokenized: {text}')
    inputs = tok(text, return_tensors="pt").to(model.device)
    out = model.generate(**inputs, max_new_tokens=60, do_sample=False)
    print("Q:", q)
    print("A:", tok.decode(out[0][inputs.input_ids.shape[1]:], skip_special_tokens=True), "\n")