FROM python:3.10-slim

# Set environment variables to ensure Python output is logged properly
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# Set the working directory
WORKDIR /app

# Install dependencies
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application
COPY . /app/

# The application runs on port 5000 locally
EXPOSE 5000

# Run the app. We use python app.py directly to ensure the background worker starts properly as defined in app.py
CMD ["python", "app.py"]
