# 1.使用ubuntu基础镜像来源
FROM ubuntu:22.04

# 2.安装过程避免交互式提示并设置主机名
ENV DEBIAN_FRONTEND=noninteractive
ENV HOSTNAME=sandbox

# 3.配置apt镜像源与重试策略
RUN sed -i 's|http://archive.ubuntu.com/ubuntu/|http://mirrors.aliyun.com/ubuntu/|g' /etc/apt/sources.list && \
    sed -i 's|http://security.ubuntu.com/ubuntu/|http://mirrors.aliyun.com/ubuntu/|g' /etc/apt/sources.list && \
    sed -i 's|http://ports.ubuntu.com/ubuntu-ports/|http://mirrors.aliyun.com/ubuntu-ports/|g' /etc/apt/sources.list && \
    echo 'Acquire::Retries "5";' > /etc/apt/apt.conf.d/80-retries && \
    echo 'Acquire::http::Timeout "120";' >> /etc/apt/apt.conf.d/80-retries && \
    echo 'APT::Get::Fix-Missing "true";' >> /etc/apt/apt.conf.d/80-retries

# 4.更新和安装基础软件后并移除依赖
RUN for i in 1 2 3; do apt-get update && break || sleep 5; done && \
    apt-get install -y --fix-missing \
    sudo bc curl wget gnupg software-properties-common supervisor \
    xterm socat xvfb x11vnc websockify\
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# 5.创建用户ubuntu并赋予sudo权限
RUN useradd -m -d /home/ubuntu -s /bin/bash ubuntu && \
    echo "ubuntu ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/ubuntu

# 6.安装Python 3.10版本(非uv)
RUN add-apt-repository ppa:deadsnakes/ppa && \
    apt-get update && \
    apt-get install -y python3.10 python3.10-venv python3.10-dev python3-pip && \
    update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.10 1 && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# 7.为pip3配置镜像源+超时+重试(构建时与运行时均生效)
RUN pip3 config set global.index-url https://mirrors.aliyun.com/pypi/simple/ && \
    pip3 config set global.timeout 120 && \
    pip3 config set global.retries 5 && \
    pip3 config set install.trusted-host mirrors.aliyun.com

# 8.安装node.js 24.12.0版本(LTS)
RUN mkdir -p /etc/apt/keyrings && \
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg && \
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_24.x nodistro main" | tee /etc/apt/sources.list.d/nodesource.list && \
    apt-get update && \
    apt-get install -y nodejs && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# 9.将npm镜像源设置为阿里云镜像源并预装常用全局包
RUN npm config set registry https://registry.npmmirror.com && \
    npm install -g docx && \
    npm cache clean --force

# 10.安装Google Chrome浏览器
RUN add-apt-repository ppa:xtradeb/apps -y && \
    apt-get update && \
    apt-get install -y chromium --no-install-recommends && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# 11.安装中文字体和语言支持
RUN for i in 1 2 3; do apt-get update && break || sleep 5; done && \
    apt-get install -y --fix-missing \
    fonts-noto-cjk \
    fonts-noto-color-emoji \
    fonts-wqy-microhei \
    language-pack-zh-hans \
    locales \
    && locale-gen zh_CN.UTF-8 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# 12.安装Skills依赖的系统工具
RUN for i in 1 2 3; do apt-get update && break || sleep 5; done && \
    apt-get install -y --fix-missing \
    poppler-utils \
    qpdf \
    pandoc \
    libreoffice-calc \
    libreoffice-writer \
    libreoffice-impress \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# 13.设置默认编码
ENV LANG=zh_CN.UTF-8 \
    LANGUAGE=zh_CN:zh \
    LC_ALL=zh_CN.UTF-8

# 13.设置工作空间
WORKDIR /sandbox

# 14.拷贝项目依赖文件到工作空间并安装依赖
# 使用链式重试+长超时应对镜像源偶发超时(pyarrow/pydantic-core等大型wheel)
COPY requirements.txt .
RUN pip3 install --no-cache-dir --timeout 300 --retries 5 -r requirements.txt || \
    (sleep 10 && pip3 install --no-cache-dir --timeout 300 --retries 5 -r requirements.txt) || \
    (sleep 10 && pip3 install --no-cache-dir --timeout 300 --retries 5 -r requirements.txt)

# 15.配置matplotlib中文字体支持
# WenQuanYi Micro Hei置首(独立ttc文件,matplotlib稳定识别);
# Noto Sans CJK SC/JP次之(TTC集合字体,matplotlib仅注册首个子字体JP,SC需fontconfig回退);
# DejaVu Sans兜底(非CJK字符)
# 同时清除所有用户的matplotlib字体缓存,确保首启动时重新扫描注册CJK字体
RUN MPL_CONFIG=$(python3 -c "import matplotlib; print(matplotlib.matplotlib_fname())") && \
    sed -i 's/^#\?font\.family:.*/font.family: sans-serif/' "$MPL_CONFIG" && \
    sed -i 's/^#\?font\.sans-serif:.*/font.sans-serif: WenQuanYi Micro Hei, Noto Sans CJK SC, Noto Sans CJK JP, DejaVu Sans, sans-serif/' "$MPL_CONFIG" && \
    sed -i 's/^#\?axes\.unicode_minus:.*/axes.unicode_minus: False/' "$MPL_CONFIG" && \
    python3 -c "import matplotlib; matplotlib.use('Agg'); import matplotlib.font_manager as fm; fm.fontManager.__init__(); fm.findfont(fm.FontProperties(family=['WenQuanYi Micro Hei']))" && \
    rm -rf /root/.cache/matplotlib /home/*/.cache/matplotlib /tmp/.fontcache

# 15.1 设置NODE_PATH使Node.js的require()能查找全局模块(默认仅查找本地node_modules)
# 放置在此处避免影响上层RUN层缓存(Chromium/pip install等耗时层保持命中)
ENV NODE_PATH=/usr/lib/node_modules

# 15.2 验证关键预装依赖是否安装成功
RUN python3 -c "import cv2; print(f'OpenCV {cv2.__version__} OK')" && \
    python3 -c "import numpy; print(f'NumPy {numpy.__version__} OK')" && \
    python3 -c "import rembg; print(f'rembg {rembg.__version__} OK')" && \
    python3 -c "import pyarrow; print(f'PyArrow {pyarrow.__version__} OK')" && \
    node -e "require('docx'); console.log('docx(npm) OK')"

# 16.拷贝当前整个项目文件
COPY . .

# 16.1 安装 matplotlib 中文字体自动配置模块
# Python 启动时通过 .pth 文件自动加载 sandbox_font_config 模块,
# 在 matplotlib 首次 import 时自动配置中文字体 + 注册 SimHei 等别名
# 覆盖 shell_execute(python3 script.py) 与 python_kernel(fork) 两条执行路径
# 防御 LLM 硬编码 `plt.rcParams['font.sans-serif'] = ['SimHei']` 导致中文乱码
#
# 实现方式:
# 1. 将自包含的 sitecustomize.py 复制为 site-packages/sandbox_font_config.py
# 2. 创建 .pth 文件触发模块加载(Python 启动时自动执行 import 语句)
# 3. 不修改系统 sitecustomize.py(Ubuntu apport hook),避免破坏系统功能
RUN SITE_PACKAGES=$(python3 -c "from distutils.sysconfig import get_python_lib; print(get_python_lib())") && \
    cp /sandbox/app/core/sitecustomize.py "$SITE_PACKAGES/sandbox_font_config.py" && \
    echo "import sandbox_font_config" > "$SITE_PACKAGES/sandbox_font_config.pth" && \
    python3 -c "import sandbox_font_config; print('sandbox_font_config OK')" && \
    python3 -c "import sandbox_font_config; print('module:', sandbox_font_config.__file__)"

# 16.2 预编译所有 .pyc 文件 - 避免运行时首次 import 时的 .py -> .pyc 编译开销
# compileall 会递归编译 /sandbox 与 /usr/local/lib/python3.10/dist-packages 下的 .py
# 对场景2 数据分析(48 次 python 调用)可节省每次 50-100ms 的 .pyc 生成时间
RUN python3 -m compileall -q /sandbox /usr/local/lib/python3.10/dist-packages 2>/dev/null || true

# 17.将supervisord.conf拷贝至supervisor服务配置目录中
COPY supervisord.conf /etc/supervisor/conf.d/sandbox.conf

# 18.暴露端口: 8080(FastAPI) 9222(CDP) 5900(vnc)
EXPOSE 8080 9222 5900 5901

# 19.额外的环境变量配置
ENV CHROME_ARGS=""
ENV UVI_ARGS=""

# 20.使用supervisor管理所有进程
CMD ["supervisord", "-n", "-c", "/sandbox/supervisord.conf"]
