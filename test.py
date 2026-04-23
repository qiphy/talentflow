from openai import OpenAI

client = OpenAI(
    api_key="sk-240d9bf0823aad42c424ffa432ba75c97af7bed1fb31f1f3",
    base_url="https://api.ilmu.ai/v1",
)

response = client.chat.completions.create(
    model="ilmu-glm-5.1",
    messages=[
        {"role": "user", "content": "Write a Python function that reverses a linked list."}
    ],
)

print(response.choices[0].message.content)