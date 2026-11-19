FROM --platform=linux/arm64 python:3.13-slim

WORKDIR /app

RUN pip install --upgrade boto3 botocore \
 && pip install "mcp>=2.1.1" \
 && pip install "aws-opentelemetry-distro>=0.10.0"

ENV PYTHONPATH=/app

EXPOSE 8000

COPY . .

CMD ["opentelemetry-instrument", "python", "-m", "mcp_server_search_note"]
