ARG PYTHON_IMAGE=python:3.11-slim
FROM ${PYTHON_IMAGE}

USER root
WORKDIR /app

RUN mkdir -p /app/data

COPY requirements.txt .

# Install Playwright/browser runtime libraries. Some devcontainer images include
# a Yarn apt source with a missing key; remove it because this service does not
# need Yarn during image build.
RUN rm -f /etc/apt/sources.list.d/yarn.list /etc/apt/sources.list.d/yarn.sources && \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
      sed -i 's@/deb.debian.org/@/mirrors.aliyun.com/@g' /etc/apt/sources.list.d/debian.sources; \
    fi && \
    if [ -f /etc/apt/sources.list ]; then \
      sed -i 's@/deb.debian.org/@/mirrors.aliyun.com/@g' /etc/apt/sources.list; \
    fi && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
      libnss3 libnspr4 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 \
      libcups2 libdrm2 libatspi2.0-0 libxcomposite1 libxdamage1 \
      libxfixes3 libxrandr2 libgbm1 libxkbcommon0 libpango-1.0-0 \
      libcairo2 libasound2 && \
    rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt

COPY src/ /app/src

ENV LOCAL_INDEX_PATH=/app/data/trailsearch.sqlite3
ENV PYTHONPATH=/app/src

EXPOSE 3000

CMD ["python", "-m", "trailsearch.main"]
