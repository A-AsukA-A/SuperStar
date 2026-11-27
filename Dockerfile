FROM python:3.13-slim

ENV PYTHONUTF8=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

COPY . /app

RUN mkdir -p /config /app/data /app/instances && \
    cp config_template.ini /config/config.ini

EXPOSE 5000
VOLUME ["/config"]

ENTRYPOINT ["python3", "main.py"]
CMD ["-c", "/config/config.ini"]
