from docx import Document
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
import re
from logger import setup_logger
import logging

setup_logger()
log = logging.getLogger("heading") 

doc = Document("ERROR_Метаморфоза.docx")

pattern = re.compile(r"^глава\s+\d+", re.IGNORECASE)

for para in doc.paragraphs:
    if pattern.match(para.text.strip()):
        para.style = doc.styles["Heading 1"]
        # разрыв страницы перед абзацем
        pPr = para._p.get_or_add_pPr()
        page_break = OxmlElement("w:pageBreakBefore")
        pPr.append(page_break)

doc.save("ERROR_Метаморфоза_с_оглавлением.docx")