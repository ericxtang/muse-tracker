FROM python:3.12-slim
WORKDIR /app
COPY build.py serve.py VERSION releases.json ./
COPY docs/agent-setup.md docs/
COPY skills skills
USER nobody
CMD ["python3", "serve.py"]
