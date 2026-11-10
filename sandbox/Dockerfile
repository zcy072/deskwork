FROM python:3.12-slim

ARG USER=agent
ARG UID=1000

# Install common Linux tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    unzip \
    zip \
    tar \
    gzip \
    xz-utils \
    procps \
    tree \
    less \
    jq \
    file \
    poppler-utils \
    bubblewrap \
    && rm -rf /var/lib/apt/lists/*

# skills 依赖的文档工具(沙箱运行时无网络,必须在构建期装好)。
# 单独一层且不装推荐包:LibreOffice 的推荐依赖会多出几百个包。
RUN apt-get update && apt-get install -y --no-install-recommends \
    qpdf \
    pandoc \
    tesseract-ocr \
    libreoffice-writer libreoffice-impress libreoffice-calc \
    fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

# Node 直接取自官方镜像,比 apt 的 nodejs+npm(几百个 node-* 包)小得多
COPY --from=node:22.23.4-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=node:22.23.4-slim /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

# JS 文档库;全局目录由 bwrap 的 NODE_PATH 暴露给 agent
RUN npm install -g docx@9.14.0 pptxgenjs@4.0.1 react-icons@5.7.0 react@19.3.1 react-dom@19.3.1 sharp@0.35.5 \
    && npm cache clean --force

# Common Python packages useful for AI agents(锁定版本:沙箱运行时无网络,重建时不应悄悄换版本)
# PIP_INDEX_URL 默认官方 PyPI;官方源慢时在 .env 里改成国内镜像
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN pip install --no-cache-dir --index-url "${PIP_INDEX_URL}" \
    # core data stack
    pandas==3.1.0 numpy==2.5.4 \
    # Excel / sheets
    openpyxl==3.1.5 xlsxwriter==3.3.0 xlrd==2.0.2 \
    # Documents
    python-docx==1.2.0 docx2txt==0.9 python-pptx==1.0.2 \
    # Images and media
    pillow==13.0.0 \
    # PDF processing
    pymupdf==2.0 pypdf==6.21.2 reportlab==5.1.0 pdfplumber==0.11.10 \
    # Plotting and Visualization
    matplotlib==3.11.2 seaborn==0.13.2 plotly==7.1.0 \
    # Web & Parsing
    beautifulsoup4==4.15.0 lxml==6.1.3 \
    # Text & Markup
    markdown-it-py==4.2.0 pyyaml==6.0.3 rich==15.0.0 tqdm==4.70.1 \
    # Data validation & config
    pydantic==2.14.1 python-dotenv==1.2.4 \
    # Utilities and ML Basics
    networkx==3.7 python-magic==0.4.27 \
    # skills 引用的提取/OCR 工具
    "markitdown[pptx,docx,xlsx,pdf]==0.1.9" pytesseract==0.3.13 pdf2image==1.17.0

# 中文 OCR 语言包。单独成层放在最后:改动它不会让上面的 npm/pip 层缓存失效。
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr-chi-sim \
    && rm -rf /var/lib/apt/lists/*

# Windows 中文字体 -> Noto CJK 的别名,让预览图接近用户机器上的效果(见文件内注释)
COPY fonts-local.conf /etc/fonts/local.conf

RUN groupadd --gid ${UID} ${USER} \
    && useradd --create-home --uid ${UID} --gid ${UID} --shell /bin/bash ${USER}

# /sandbox_data is a runtime bind mount (see docker-compose.yml); any dirs or
# permissions set here would be masked by the mount, so per-session dirs are
# created on the host and their ownership/mode is fixed at runtime by
# sandbox-manager (/provision).
WORKDIR /sandbox_data

USER ${USER}

CMD ["sleep", "infinity"]
