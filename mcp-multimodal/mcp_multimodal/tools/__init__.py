from .image_understand import register_image_understand
from .browser_image import register_browser_image
from .ocr_extract import register_ocr_extract
from .speech2text import register_speech2text
from .video_analyse import register_video_analyse
from .pdf_parse import register_pdf_parse
from .ppt_parse import register_ppt_parse
from .image_create import register_image_create

ALL_TOOL_REGISTRARS = [
    register_image_understand,
    register_browser_image,
    register_ocr_extract,
    register_speech2text,
    register_video_analyse,
    register_pdf_parse,
    register_ppt_parse,
    register_image_create,
]
