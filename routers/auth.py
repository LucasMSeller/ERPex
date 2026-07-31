from fastapi import APIRouter, Query, HTTPException, BackgroundTasks
from fastapi.responses import RedirectResponse, HTMLResponse
from services.meli_service import MeliService
from services.token_store import TokenStore
from services.sheets_service import SheetsService
from services.sync_service import sync_one_company

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/meli/login")
async def meli_login(
    company: str = Query("", description="Nome da loja (opcional). Vazio = usa o nickname do ML"),
):
    """Inicia o OAuth da app única. O nome da loja vai no parâmetro state."""
    url = MeliService.get_oauth_url(company_key=company)
    return RedirectResponse(url)


@router.get("/meli/callback")
async def meli_callback(
    background_tasks: BackgroundTasks,
    code: str = Query(...),
    state: str = Query("", description="nome da loja vindo do login (opcional)"),
):
    try:
        tokens = await MeliService.exchange_code(code)
    except Exception as e:
        raise HTTPException(400, f"Erro ao trocar code por token: {e}")

    user_id = str(tokens.get("user_id", ""))

    # Busca o nickname da loja no ML
    nickname = ""
    try:
        store_tmp = {"access_token": tokens["access_token"], "user_id": user_id}
        me = await MeliService(store_tmp)._get("/users/me")
        nickname = me.get("nickname", "")
        if not user_id:
            user_id = str(me.get("id", ""))
    except Exception:
        pass

    # Nome da loja = state informado no login, senão o nickname do ML
    store_name = (state.strip() or nickname or f"Loja {user_id}").strip()

    # Cria a aba da loja a partir do modelo (se ainda não existir)
    sheets = SheetsService()
    try:
        sheets.ensure_store_tab(store_name)
    except Exception as e:
        raise HTTPException(500, f"Erro ao criar a aba da loja: {e}")

    await TokenStore().save_store(
        user_id=user_id,
        company_key=store_name,
        sheet_tab=store_name,
        access_token=tokens["access_token"],
        refresh_token=tokens.get("refresh_token", ""),
        nickname=nickname,
    )

    # Já importa os anúncios da loja em segundo plano
    background_tasks.add_task(sync_one_company, store_name)

    return HTMLResponse(f"""
        <html><body style="font-family:sans-serif;text-align:center;padding-top:60px">
        <h2>✅ Loja conectada com sucesso!</h2>
        <p><b>Aba criada/usada:</b> {store_name}</p>
        <p><b>Conta ML:</b> {nickname} (user_id {user_id})</p>
        <p>Tokens salvos. Importação dos anúncios iniciada — aguarde alguns minutos
        e veja a aba se preencher.</p>
        </body></html>
    """)
