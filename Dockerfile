FROM python:3.9-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY partpilot ./partpilot
COPY scripts ./scripts
COPY data/sample ./data/sample
ENV PARTPILOT_HOST=0.0.0.0 PARTPILOT_RAW_DIR=data/sample LLM_MODE=demo
EXPOSE 18765
# 啟動時：建帳號 → 清理 → 啟動網頁；real 模式仍經相同初始化。
CMD ["bash", "scripts/demo.sh", "--configured-mode"]
