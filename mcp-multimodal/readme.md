mcp-multimodal/
├── pyproject.toml
├── Dockerfile
├── run.sh
├── config.yaml          # 多模态模型可配置
├── mcp_multimodal/
│   ├── __init__.py
│   ├── server.py        # MCP服务入口(FastMCP + streamable-http)
│   ├── config.py        # 配置管理
│   ├── client.py        # BigModel API客户端
│   ├── tools/
│   │   ├── __init__.py
│   │   ├── image_understand.py   # 图像理解
│   │   ├── browser_image.py      # 网页阅读
│   │   ├── ocr_extract.py        # OCR文字提取
│   │   ├── speech2text.py        # 语音转文本
│   │   ├── video_analyse.py      # 视频摘要&关键帧
│   │   ├── pdf_parse.py          # 图文PDF解析
│   │   ├── ppt_parse.py          # 图文PPT解析
│   │   └── image_create.py       # 图像生成
│   └── utils/
│       ├── __init__.py
│       ├── file_utils.py         # 文件处理工具
│       └── url_utils.py          # URL处理工具
└── tests/
    ├── __init__.py
    ├── test_config.py
    ├── test_client.py
    └── test_tools.py