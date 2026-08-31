import os
from huggingface_hub import InferenceClient
import dotenv

dotenv.load_dotenv()

client = InferenceClient(
    provider="hf-inference",
    api_key=os.environ["HF_TOKEN"],
)

result = client.sentence_similarity(
    "That is a happy person",
    [
        "That is a happy dog",
        "That is a very happy person",
        "Today is a sunny day"
    ],
    model="BAAI/bge-m3",
)

print(result)