import os
import json
import anthropic
import gspread
import asyncio
from aiohttp import web
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from google.oauth2.service_account import Credentials
import pdfplumber
import pandas as pd

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
GOOGLE_CREDS = os.environ["GOOGLE_CREDS"]

CATEGORIES = """
Статьи (колонка F): Логистика, Швеи, Реклама ВБ, Реклама, Фотосъемка, Аренда,
Менеджер ВБ, Ткани, Комплектующие, Связь интернет адм., Связь интернет пр.,
ПО и IT (1С и прочее), Конструктор и образцы, Обслуживание РС в банке,
Канц.товары хоз.товары, Выручка Wildberries, Выручка OZON, Выручка ОПТ,
Комиссия ВБ, Комиссия OZON, Логистика ВБ, Услуги хранения ВБ,
С/с проданных товаров Wildberries, C/с проданных товаров OZON, С/с ОПТ,
Налог на доход, Амортизация, Консалтинговые услуги, Закройщик, Помощник,
Бух.услуги (декларация), Уборка, Оборудование (крупное), Штрафы ВБ,
Возвраты от клиентов, Прочие расходы

Направления (колонка G): Выручка, Себестоимость, ФОТ, Маркетинг и реклама,
Расходы по ВБ и ОЗОН, ПО и IT, Прочие расходы, Помещения, Банковские услуги,
Расходы ниже опер.прибыли, Капитализация
"""

def get_sheet():
    creds_dict = json.loads(GOOGLE_CREDS)
    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    client = gspread.authorize(creds)
    return client.open_by_key(SPREADSHEET_ID).worksheet("Журнал операций")

def parse_bank_statement(text: str) -> list:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = f"""Ты финансовый ассистент. Тебе дана банковская выписка.
Разбери каждую операцию и верни JSON-массив.

Для каждой операции:
- date_dds: дата платежа (ДД.ММ.ГГГГ)
- date_pnl: дата начисления (обычно = date_dds)
- amount: сумма (расходы со знаком минус, доходы положительные)
- article: статья из списка
- direction: направление из списка
- comment: контрагент или назначение платежа

{CATEGORIES}

Правила:
- Поступления от ВБ → Выручка Wildberries / Выручка
- Поступления от OZON → Выручка OZON / Выручка
- СДЭК, Байкал-сервис → Логистика / Прочие расходы
- Переводы физлицам крупные → Швеи / ФОТ
- ФНС, налоговая → Налог на доход / Расходы ниже опер.прибыли
- Банковские комиссии → Обслуживание РС в банке / Банковские услуги
- Аренда помещения → Аренда / Помещения

Верни ТОЛЬКО валидный JSON массив без пояснений:
[{{"date_dds":"01.01.2025","date_pnl":"01.01.2025","amount":"-5000","article":"Логистика","direction":"Прочие расходы","comment":"СДЭК"}}]

Выписка:
{text}"""

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}]
    )
    result = response.content[0].text.strip()
    if result.startswith("```"):
        result = result.split("```")[1]
        if result.startswith("json"):
            result = result[4:]
    return json.loads(result.strip())

def extract_text(file_path: str) -> str:
    if file_path.endswith(".pdf"):
        with pdfplumber.open(file_path) as pdf:
            return "\n".join(p.extract_text() or "" for p in pdf.pages)
    elif file_path.endswith((".xlsx", ".xls")):
        df = pd.read_excel(file_path)
        return df.to_string()
    else:
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read()

def write_to_sheet(operations: list) -> int:
    sheet = get_sheet()
    rows = []
    for op in operations:
        rows.append([
            "", "",
            op.get("date_dds", ""),
            op.get("date_pnl", ""),
            op.get("amount", ""),
            op.get("article", ""),
            op.get("direction", ""),
            "",
            op.get("comment", "")
        ])
    sheet.append_rows(rows)
    return len(rows)

async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Обрабатываю выписку, подожди...")
    try:
        file = await update.message.document.get_file()
        file_name = update.message.document.file_name
        file_path = f"/tmp/{file_name}"
        await file.download_to_drive(file_path)
        text = extract_text(file_path)
        operations = parse_bank_statement(text)
        count = write_to_sheet(operations)
        await update.message.reply_text(
            f"✅ Готово! Добавлено {count} операций в журнал.\n"
            f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {str(e)}")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! 👋\nОтправь мне выписку из банка в формате PDF или Excel, "
        "и я разнесу операции по статьям в таблицу управленческого учёта."
    )

async def health(request):
    return web.Response(text="OK")

async def main():
    port = int(os.environ.get("PORT", 8080))
    web_app = web.Application()
    web_app.router.add_get("/", health)
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.Document.ALL, handle_file))
    app.add_handler(MessageHandler(filters.TEXT, handle_text))
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
