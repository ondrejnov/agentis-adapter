FROM python:3.13-slim

# Set environment variables
ENV TZ="Europe/Prague" \
    DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=off \
    PIP_DISABLE_PIP_VERSION_CHECK=on \
    PIP_DEFAULT_TIMEOUT=100 \
    POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_CREATE=false \
    POETRY_CACHE_DIR='/var/cache/pypoetry' \
    POETRY_HOME='/usr/local' \
    POETRY_VERSION=1.8.2 \
    FASTEMBED_CACHE_PATH='/opt/fastembed_cache' \
    HF_HOME='/opt/huggingface_cache'

# Install system dependencies and Poetry in a single layer
# Clean up apt cache to reduce image size
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    python3-dev \
    default-libmysqlclient-dev \
    build-essential \
    pkg-config \
    python3-mysqldb \
    curl \
    jq \
    ca-certificates \
    openssh-client \
    git \
    mc \
    ffmpeg \
    postgresql-client \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libgdk-pixbuf-2.0-0 \
    libffi8 \
    shared-mime-info \
    libgobject-2.0-0 \
    python3-tk \
    tzdata && \
    ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && \
    echo $TZ > /etc/timezone && \
    curl -sSL https://install.python-poetry.org | python3 - && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && ln -sf /usr/lib/x86_64-linux-gnu/libmysqlclient.so /usr/lib/x86_64-linux-gnu/libmysqlclient.so.21 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

RUN curl -LO https://github.com/BurntSushi/ripgrep/releases/download/14.1.1/ripgrep_14.1.1-1_amd64.deb \
    && dpkg -i ripgrep_14.1.1-1_amd64.deb \
    && rm ripgrep_14.1.1-1_amd64.deb

RUN curl -LsSf https://astral.sh/uv/install.sh | sh && \
    ln -s /root/.local/bin/uv /usr/local/bin/uv && \
    ln -s /root/.local/bin/uvx /usr/local/bin/uvx

# Install GitHub CLI (gh)
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | gpg --dearmor -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | tee /etc/apt/sources.list.d/github-cli.list > /dev/null \
    && apt-get update \
    && apt-get install -y gh \
    && rm -rf /var/lib/apt/lists/*

# Create the .ssh directory and add GitHub to known_hosts
RUN mkdir -p /root/.ssh \
    && ssh-keyscan github.com >> /root/.ssh/known_hosts

RUN git config --global user.name "Agentis"
RUN git config --global user.email "novakondrej@seznam.cz"

RUN npm install -g agent-browser
RUN agent-browser install --with-deps
RUN npx playwright install --with-deps chrome

RUN git clone https://github.com/ondrejnov/code-review-graph.git
RUN pip install -e ./code-review-graph[communities,embeddings]
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"

COPY agdoc /opt/agdoc
RUN pip install /opt/agdoc
COPY agentis-adapter /opt/agentis-adapter
RUN pip install /opt/agentis-adapter
RUN python -c "from fastembed import LateInteractionTextEmbedding, SparseTextEmbedding; list(SparseTextEmbedding(model_name='Qdrant/bm25').embed(['agdoc cache probe'])); list(LateInteractionTextEmbedding(model_name='answerdotai/answerai-colbert-small-v1').embed(['agdoc cache probe']))" \
    && HF_HUB_OFFLINE=1 python -c "from fastembed import LateInteractionTextEmbedding, SparseTextEmbedding; list(SparseTextEmbedding(model_name='Qdrant/bm25', local_files_only=True).embed(['agdoc cache probe'])); list(LateInteractionTextEmbedding(model_name='answerdotai/answerai-colbert-small-v1', local_files_only=True).embed(['agdoc cache probe']))" \
    && chmod -R a+rX /opt/fastembed_cache /opt/huggingface_cache

RUN npm install -g pyright

RUN npm install -g @fission-ai/openspec@latest
RUN curl -fsSL https://raw.githubusercontent.com/iOfficeAI/OfficeCLI/main/install.sh | bash
COPY ./rtk /root/.local/bin

COPY clarp /opt/clarp
RUN cd /opt/clarp && npm install && npm run build && npm install -g .

RUN npm install prettier -g
RUN npm install -g @anthropic-ai/claude-code@2.1.167
RUN npm install -g opencode-ai@1.16.2 opencode-linux-x64-baseline@1.16.2

CMD ["opencode", "web", "--hostname", "0.0.0.0"]
