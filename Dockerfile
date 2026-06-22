# backend/Dockerfile

# 1. Use the official Python image, 'slim' version to reduce image size and build time
FROM python:3.11-slim

# 2. Set the working directory inside the container
WORKDIR /app

# 3. Environment variables to optimize Python within Docker
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# 4. Install necessary operating system dependencies (some are essential to compile ChromaDB/C++)
RUN apt-get update && apt-get install -y \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# 5. Copy the requirements file and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 6. Copy the rest of the source code to the /app directory in the container
COPY . .

# 7. Expose the port that Render usually uses by default (10000)
EXPOSE 10000

# 8. Command to start the uvicorn asynchronous web server
# Binds to the 0.0.0.0 host to accept external connections within the cloud environment
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "10000"]