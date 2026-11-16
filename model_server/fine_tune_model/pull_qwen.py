from transformers import AutoTokenizer, AutoModelForCausalLM
import torch

model_name = "qwen/qwen3-0.6B"
print(f"Loading model: {model_name}")
tokenizer = AutoTokenizer.from_pretrained(model_name)
model = AutoModelForCausalLM.from_pretrained(model_name, trust_remote_code=True,torch_dtype=torch.float16, device_map="auto")
if model is not None:
    print(f"Model {model_name} loaded successfully.")
print(f'model parameters: {sum(p.numel() for p in model.parameters())}')
if torch.cuda.is_available():
    print(
        "GPU memory allocated:",
        round(torch.cuda.memory_allocated() / 1024**3, 2),
        "GB"
    )
messages = [
    {
        "role": "system",
        "content": "You are a friendly AI assistant.Your name is a Kodeara. Keep your answers short and natural."
    },
    {
        "role": "user",
        "content": "Hello! How are you?"
    }
]
text = tokenizer.apply_chat_template(
    messages,
    tokenize=False,
    add_generation_prompt=True,
    enable_thinking=False
)
with torch.inference_mode():
    inputs = tokenizer(text,return_tensors='pt').to(model.device)
    outputs = model.generate(**inputs,max_new_tokens=100,do_sample=True,temperature=0.7,top_p=0.9)
new_tokens = outputs[0][inputs.input_ids.shape[1]:]
response = tokenizer.decode(new_tokens,skip_special_tokens=True)
print(f'response {response}')
