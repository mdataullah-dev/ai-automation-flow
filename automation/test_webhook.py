import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()

WEBHOOK_URL = os.getenv("MAKE_WEBHOOK_URL")

if not WEBHOOK_URL:
    print("Error: MAKE_WEBHOOK_URL is missing in .env file!")
    sys.exit(1)

# Test data to send to the webhook
payload = {
    "batch_id": "chunk_01",
    "people": [
        {"id": 1, "name": "Nikhil Chopra", "skills": "n8n, Pandas, SQL"},
        {"id": 2, "name": "Sneha Mishra", "skills": "Docker, MySQL, Pandas, Selenium"},
    ],
}

print("sending test data to Make.com...")
response = requests.post(WEBHOOK_URL, json=payload)

print(f"Status Code: {response.status_code}")
print(f"Response: {response.text}")
