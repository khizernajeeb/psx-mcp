FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY psx_mcp ./psx_mcp
ENV PORT=7860
EXPOSE 7860
CMD ["python", "-m", "psx_mcp.server"]
