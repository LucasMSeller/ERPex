"""Instância única do Jinja2Templates, compartilhada pelos routers de página."""
from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory="templates")
