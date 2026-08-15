FROM python:3.10-slim

# Cài đặt ffmpeg và git
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg git ca-certificates && \
    rm -rf /var/lib/apt/lists//*

# Tạo user không phải root theo chuẩn Hugging Face
RUN useradd -m -u 1000 user
USER user
ENV PATH="/home/user/.local/bin:$PATH"

WORKDIR /app

# Cài đặt thư viện Python
COPY --chown=user:user requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Sao chép mã nguồn
COPY --chown=user:user . .

# Port mặc định cho Hugging Face Spaces
EXPOSE 7860

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "7860"]
