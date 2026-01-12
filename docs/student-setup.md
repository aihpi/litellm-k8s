# Student Setup

## Getting an API key

- Request an API key from the course staff
- Keep it private and do not share publicly

## Using the gateway

```bash
curl -H "Authorization: Bearer YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "llama-3b", "messages": [{"role": "user", "content": "Hello"}]}' \
  http://GATEWAY_HOST/v1/chat/completions
```
