# site.py (ПОЛНЫЙ КОД, ВСЕ ФУНКЦИИ РЕАЛИЗОВАНЫ И АДАПТИРОВАНЫ)

import logging
import datetime
import os
import re
import json
from typing import List, Dict, Any, Optional, Tuple

# --- Необходимые внешние библиотеки ---
import httpx
from bs4 import BeautifulSoup

# --- Асинхронные веб-библиотеки ---
from starlette.applications import Starlette
from starlette.routing import Route, Mount
from starlette.responses import JSONResponse, HTMLResponse, RedirectResponse
from starlette.requests import Request
from starlette.exceptions import HTTPException
from starlette.templating import Jinja2Templates
from starlette.staticfiles import StaticFiles
import uvicorn
from urllib.parse import quote, unquote

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

# --- ГЛОБАЛЬНЫЕ КОНСТАНТЫ И НАСТРОЙКИ ---
REPLACEMENTS_URLS = [
    "https://menu.sttec.yar.ru/timetable/rasp_first.html",
    "https://menu.sttec.yar.ru/timetable/rasp_second.html"
]
DEFAULT_SCHEDULE_FORMAT = "%NUM% %LESSON% (%ROOM%)"
COOLDOWN_MINUTES = 30
REPLACEMENTS_HEADERS = [
    "№", "Группа", "Номер_пары", "Дисциплина_по_расписанию",
    "Дисциплина_по_замене", "Аудитория"
]

# --- КЭШИРОВАНИЕ ДАННЫХ ---
REPLACEMENTS_CACHE: Dict[str, Any] = {
    "replacements": [], "date_info": "Неизвестно", "date_object": None,
    "last_fetch_time": datetime.datetime.min, "errors": []
}
MERGED_SCHEDULE_CACHE: Dict[str, List[Dict[str, Any]]] = {}

# --- РАСПИСАНИЕ (Загружается при старте) ---
SCHEDULE: Dict[str, Any] = {}
TEACHERS_SCHEDULE: Dict[str, Any] = {}

# --- Настройка шаблонов Jinja2 ---
templates = Jinja2Templates(directory='templates')
templates.env.globals['quote'] = quote
templates.env.globals['unquote'] = unquote


# ====================================================================
# БЛОК 2: SCHEDULE_CORE (Полная логика обработки данных)
# ====================================================================

def load_schedule_data(file_path: str = 'schedule.json'):
    """Загружает расписание групп из JSON файла в память."""
    global SCHEDULE
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            SCHEDULE = json.load(f)
        logger.info(f"✅ Расписание групп загружено: {len(SCHEDULE)} записей.")
    except Exception as e:
        logger.error(f"❌ Критическая ошибка загрузки расписания: {e}")
        SCHEDULE = {}


def parse_russian_date(date_string: str) -> Optional[datetime.date]:
    """Парсит дату из строки формата 'на 5 ноября 2025 года'."""
    match = re.search(
        r'на (\d{1,2}) (января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря) (\d{4}) года',
        date_string)
    if not match: return None
    day, month_name, year = match.groups()
    month_map = {'января': 1, 'февраля': 2, 'марта': 3, 'апреля': 4, 'мая': 5, 'июня': 6, 'июля': 7, 'августа': 8,
                 'сентября': 9, 'октября': 10, 'ноября': 11, 'декабря': 12}
    month = month_map.get(month_name)
    try:
        return datetime.date(int(year), month, int(day))
    except (ValueError, TypeError):
        return None


async def fetch_replacements_data(force_update: bool = False) -> Dict[str, Any]:
    """Асинхронно загружает и кэширует данные о заменах."""
    time_since_last_fetch = datetime.datetime.now() - REPLACEMENTS_CACHE['last_fetch_time']
    if not force_update and time_since_last_fetch < datetime.timedelta(minutes=COOLDOWN_MINUTES):
        return REPLACEMENTS_CACHE

    logger.info(f"⏳ Выполняется запрос к серверам замен: {REPLACEMENTS_URLS}")
    all_replacements_data: List[Dict[str, Any]] = []
    primary_date_info = "Дата не указана"
    primary_date_object: Optional[datetime.date] = None
    fetch_errors: List[str] = []

    async with httpx.AsyncClient(timeout=15) as client:
        for i, url in enumerate(REPLACEMENTS_URLS):
            source_shift = f"{i + 1}-ая смена"
            try:
                response = await client.get(url)
                response.raise_for_status()
                response.encoding = 'utf-8'
                soup = BeautifulSoup(response.text, 'html.parser')

                date_info_tag = soup.find(lambda tag: tag.text and 'изменения' in tag.text.lower())
                date_info_text = date_info_tag.text.strip() if date_info_tag else 'Дата не указана'
                table = soup.find('table')
                if not table:
                    fetch_errors.append(f"❌ {source_shift}: Таблица замен не найдена.")
                    continue

                if i == 0:
                    primary_date_info = date_info_text
                    primary_date_object = parse_russian_date(date_info_text)

                for row in table.find_all('tr'):
                    cells = row.find_all('td')
                    if len(cells) == len(REPLACEMENTS_HEADERS):
                        row_data = {REPLACEMENTS_HEADERS[j]: cells[j].text.strip() for j in
                                    range(len(REPLACEMENTS_HEADERS))}
                        all_replacements_data.append(row_data)
            except Exception as e:
                fetch_errors.append(f"❌ Ошибка при запросе к {source_shift}: {str(e)}")

    REPLACEMENTS_CACHE.update({
        "replacements": all_replacements_data, "date_info": primary_date_info,
        "date_object": primary_date_object, "last_fetch_time": datetime.datetime.now(),
        "errors": fetch_errors
    })
    logger.info(
        f"✅ Кэш замен обновлен в {REPLACEMENTS_CACHE['last_fetch_time']:%H:%M:%S}. Найдено {len(all_replacements_data)} записей.")
    return REPLACEMENTS_CACHE


def get_week_type() -> bool:
    """Определяет четность недели."""
    return datetime.date.today().isocalendar()[1] % 2 == 0


def get_week_type_display(week_type: bool) -> str:
    """Возвращает русское название типа недели."""
    return "числитель" if week_type else "знаменатель"


def get_teacher_from_lesson(lesson_name: str) -> Tuple[str, str]:
    """Извлекает преподавателя из скобок в названии пары."""
    teacher_match = re.search(r'\((.*?)\)', lesson_name)
    if teacher_match:
        teacher_display = teacher_match.group(1).strip()
        lesson_display = lesson_name.replace(teacher_match.group(0), "").strip()
        return lesson_display, teacher_display
    return lesson_name.strip(), 'Не указан'


def get_day_schedule(schedule_data: Dict[str, Any], group_name: str, day_name: str, week_type: bool) -> List[
    Dict[str, Any]]:
    """Извлекает базовое расписание на день, фильтруя по типу недели."""
    if group_name not in schedule_data: return []
    day_schedule = schedule_data[group_name].get(day_name, [])
    filtered_pairs = []
    for pair in day_schedule:
        pair_type = pair.get('type', 'Еженедельно')
        is_current = (pair_type == 'Еженедельно') or \
                     (pair_type == 'Четная' and week_type) or \
                     (pair_type == 'Нечетная' and not week_type)
        if is_current and pair.get('lesson') != '(Нет пары)':
            new_pair = pair.copy()
            new_pair['is_replacement'] = False
            new_pair['old_lesson'] = new_pair['lesson']
            new_pair['old_classroom'] = new_pair.get('classroom', 'Не указана')

            lesson_name, teacher_name = get_teacher_from_lesson(new_pair['lesson'])
            new_pair['lesson'] = lesson_name
            new_pair['teacher'] = teacher_name if teacher_name != 'Не указан' else new_pair.get('teacher', 'Не указан')

            filtered_pairs.append(new_pair)
    return sorted(filtered_pairs, key=lambda x: int(x.get('pair_num', 0)))


def apply_replacements_to_schedule(base_schedule: List[Dict[str, Any]], all_replacements: List[Dict[str, Any]],
                                   entity_name: str, is_teacher: bool) -> List[Dict[str, Any]]:
    """Применяет замены к базовому расписанию для сущности (группы или преподавателя)."""
    if not all_replacements: return base_schedule
    merged_schedule = [pair.copy() for pair in base_schedule]
    replacements_dict = {}

    for replacement in all_replacements:
        pair_num = replacement.get('Номер_пары')
        group_raw = replacement.get('Группа', '')
        if pair_num and group_raw:
            for group in group_raw.split('/'):
                replacements_dict[(group.strip(), str(pair_num))] = replacement

    for pair in merged_schedule:
        pair_num = str(pair.get('pair_num'))
        user_groups = [g.strip() for g in entity_name.split('/')] if not is_teacher else [pair.get('group', '???')]

        for group in user_groups:
            replacement = replacements_dict.get((group, pair_num))
            if replacement:
                new_lesson_raw = replacement.get('Дисциплина_по_замене', 'Неизвестно')
                new_classroom = replacement.get('Аудитория', 'Не указана')
                new_lesson, new_teacher = get_teacher_from_lesson(new_lesson_raw)
                is_cancellation = "❌ (Отмена/Перенос)" in new_lesson_raw

                pair['lesson'] = new_lesson
                pair['classroom'] = new_classroom
                pair['teacher'] = new_teacher if new_teacher != 'Не указан' else pair.get('teacher', 'Не указан')
                pair['is_replacement'] = True
                pair['is_cancellation'] = is_cancellation
                break

    return merged_schedule


async def get_merged_daily_schedule(target_date: datetime.date, entity_name: str, is_teacher: bool = False) -> List[
    Dict[str, Any]]:
    """Получает расписание на день с учетом замен и кэширует результат."""
    cache_key = f"{target_date.isoformat()}:{'teacher' if is_teacher else 'group'}:{entity_name}"
    if cache_key in MERGED_SCHEDULE_CACHE:
        return MERGED_SCHEDULE_CACHE[cache_key]

    day_name = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"][
        target_date.weekday()]
    week_type = get_week_type()
    schedule_source = TEACHERS_SCHEDULE if is_teacher else SCHEDULE
    base_schedule = get_day_schedule(schedule_source, entity_name, day_name, week_type)

    replacements_data = await fetch_replacements_data()
    current_replacements = replacements_data['replacements'] if replacements_data['date_object'] == target_date else []

    merged_schedule = apply_replacements_to_schedule(base_schedule, current_replacements, entity_name, is_teacher)
    MERGED_SCHEDULE_CACHE[cache_key] = merged_schedule
    return merged_schedule


def get_schedule_for_display(group_name: str, target_view: str, replacements_data: Dict[str, Any]) -> Tuple[
    Dict[str, Any], str, str]:
    """Генерирует расписание для HTML-отображения."""
    replacements_date_obj = replacements_data.get('date_object')
    replacements_list = replacements_data.get('replacements', [])
    today = datetime.date.today()
    days = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота"]
    monday = today - datetime.timedelta(days=today.weekday())

    full_schedule_raw = {}
    for day_index, day_name in enumerate(days):
        day_date = monday + datetime.timedelta(days=day_index)
        current_replacements = replacements_list if replacements_date_obj == day_date else []
        base_schedule = get_day_schedule(SCHEDULE, group_name, day_name, get_week_type())
        merged_schedule = apply_replacements_to_schedule(base_schedule, current_replacements, group_name,
                                                         is_teacher=False)
        full_schedule_raw[day_name] = merged_schedule

    full_schedule = {}
    if target_view == 'week':
        full_schedule = full_schedule_raw
        display_title = "Расписание на Неделю"
    else:
        target_date = today if target_view == 'today' else today + datetime.timedelta(days=1)
        if target_date.weekday() >= 6:
            target_date = today + datetime.timedelta(days=(7 - today.weekday()))

        day_name = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"][
            target_date.weekday()]

        display_title = f"Расписание на {'Сегодня' if target_view == 'today' else 'Завтра'}, {target_date.strftime('%d.%m')}"
        full_schedule = {day_name: full_schedule_raw.get(day_name, [])}

    replacements_applied_to = f"Замены применены к {replacements_date_obj.strftime('%d.%m')}" if replacements_date_obj else "Замены не найдены"

    return full_schedule, display_title, replacements_applied_to


def format_schedule_to_kwgt_text(schedule: List[Dict[str, Any]], week_type: str, custom_format: str) -> str:
    """Форматирует расписание в одну строку, используя \n для переноса (KWGT-совместимо)."""
    result_lines = []
    if not schedule:
        return f"[c=d35400]Неделя: {week_type}[/c] [c=3498db]| {datetime.date.today().strftime('%d.%m')}[/c]\n\n🎉 Пар/занятий нет."

    for pair in schedule:
        is_cancellation = pair.get('is_cancellation', False)
        is_replacement = pair.get('is_replacement', False)

        replacements = {
            "%NUM%": str(pair.get('pair_num', '?')), "%TEACHER%": pair.get('teacher', 'Н/У'),
            "%BR%": "\n", "%LESSON%": pair.get('lesson', 'Не указано'),
            "%ROOM%": pair.get('classroom', 'Н/У'), "%OLD_ROOM%": pair.get('old_classroom', 'Н/У'),
            "%STATUS%": "",
        }

        formatted_line = custom_format
        if is_cancellation:
            replacements["%STATUS%"] = "ОТМЕНА"
            replacements["%LESSON%"] = pair.get('old_lesson', '???')
            replacements["%ROOM%"] = pair.get('old_classroom', 'Н/У')
            formatted_line = f"[c=e74c3c]🚫 {formatted_line}[/c]"
        elif is_replacement:
            replacements["%STATUS%"] = "ЗАМЕНА"
            formatted_line = f"[c=f39c12]🔄 {formatted_line}[/c]"

        for key, value in replacements.items():
            formatted_line = formatted_line.replace(key, value)

        # Удаление неиспользованных тегов и лишних пробелов
        formatted_line = re.sub(r'\[\]', '', formatted_line).strip()

        # Удаление %OLD_ROOM% и %STATUS% в конце, если они не были использованы
        formatted_line = formatted_line.replace("%OLD_ROOM%", "").replace("%STATUS%", "")

        result_lines.append(formatted_line)

    header_line = f"[c=d35400]Неделя: {week_type}[/c] [c=3498db]| {datetime.date.today().strftime('%d.%m')}[/c]\n"
    return header_line + "\n".join(result_lines)


# ====================================================================
# БЛОК 3: WEB_APP & API (НА STARLETTE)
# ====================================================================

async def root_redirect(request: Request):
    """Корневой маршрут перенаправляет на список групп."""
    return RedirectResponse(url="/groups")


async def list_groups_handler(request: Request):
    """Главная страница: список всех групп с поиском/фильтрацией."""
    search_term = request.query_params.get('search', '').strip()
    all_groups = sorted(SCHEDULE.keys())
    if search_term:
        search_lower = search_term.lower()
        groups = [g for g in all_groups if search_lower in g.lower()]
    else:
        groups = all_groups
    context = {'request': request, 'groups': groups, 'search_term': search_term}
    return templates.TemplateResponse("group_list_template.html", context=context)


async def show_schedule_handler(request: Request):
    """Страница с расписанием для конкретной группы."""
    group_name = unquote(request.path_params['group_name_encoded'])
    view_type = request.query_params.get('view_type', 'today')
    if group_name not in SCHEDULE:
        raise HTTPException(status_code=404, detail="Group not found")

    replacements_data = await fetch_replacements_data()
    full_schedule, display_title, replacements_applied_to = get_schedule_for_display(group_name, view_type,
                                                                                     replacements_data)

    context = {
        'request': request, 'group_name': group_name,
        'group_name_encoded': quote(group_name, safe=''),
        'schedule': full_schedule, 'view_type': view_type,
        'display_title': display_title, 'replacements_applied_to': replacements_applied_to,
        'week_type_display': get_week_type_display(get_week_type()),
        'cache_time': REPLACEMENTS_CACHE['last_fetch_time'].strftime("%H:%M:%S")
    }
    return templates.TemplateResponse("schedule_view_template.html", context=context)


async def api_replacements_date_handler(request: Request):
    """API-эндпоинт: Возвращает дату, на которую действуют замены."""
    try:
        replacements_data = await fetch_replacements_data()
        date_obj = replacements_data.get('date_object')
        response_data = {
            "is_available": bool(date_obj and replacements_data.get('replacements')),
            "replacements_date": date_obj.isoformat() if date_obj else None,
            "date_info_text": replacements_data.get('date_info', 'Неизвестно'),
            "last_cache_update": REPLACEMENTS_CACHE['last_fetch_time'].strftime("%H:%M:%S"),
            "errors": replacements_data.get('errors', [])
        }
        return JSONResponse(response_data)
    except Exception as e:
        logger.error(f"❌ Ошибка API /api/replacements_date: {e}")
        return JSONResponse({"error": "Internal Server Error", "details": str(e)}, status_code=500)


async def api_schedule_by_date_handler(request: Request):
    """API-эндпоинт: Возвращает расписание для группы на конкретную дату (YYYY-MM-DD)."""
    group_name = unquote(request.path_params['group_name_encoded']).strip()
    target_date_str = request.query_params.get('date')

    if not target_date_str:
        return JSONResponse({"error": "Query parameter 'date=YYYY-MM-DD' is required."}, status_code=400)
    try:
        target_date = datetime.date.fromisoformat(target_date_str)
    except ValueError:
        return JSONResponse({"error": "Invalid date format. Use YYYY-MM-DD."}, status_code=400)
    if group_name not in SCHEDULE:
        return JSONResponse({"error": f"Group '{group_name}' not found."}, status_code=404)

    try:
        schedule_data = await get_merged_daily_schedule(target_date, group_name)
        response_data = {
            "query_group": group_name, "target_date": target_date.isoformat(),
            "week_type_ru": get_week_type_display(get_week_type()),
            "schedule": schedule_data
        }
        return JSONResponse(response_data)
    except Exception as e:
        logger.error(f"❌ Ошибка API при получении расписания для {group_name} на {target_date_str}: {e}")
        return JSONResponse({"error": "Internal Server Error", "details": str(e)}, status_code=500)


async def api_schedule_today_text_handler(request: Request):
    """API-эндпоинт: Возвращает расписание для группы на СЕГОДНЯ в виде одной строки текста (для KWGT)."""
    group_name = unquote(request.path_params['group_name_encoded']).strip()
    target_date = datetime.date.today()
    if group_name not in SCHEDULE: return HTMLResponse("Error: Group not found.", status_code=404)

    try:
        schedule_data = await get_merged_daily_schedule(target_date, group_name)
        week_type = get_week_type_display(get_week_type())
        text_output = format_schedule_to_kwgt_text(schedule_data, week_type, DEFAULT_SCHEDULE_FORMAT)
        return HTMLResponse(text_output, media_type="text/plain")
    except Exception as e:
        logger.error(f"❌ Ошибка API KWGT: {e}")
        return HTMLResponse("Error: Internal Server Error", status_code=500)


async def api_schedule_for_replacements_handler(request: Request):
    """API-эндпоинт: Возвращает расписание для группы на дату, на которую действуют замены (JSON)."""
    group_name = unquote(request.path_params['group_name_encoded']).strip()
    if group_name not in SCHEDULE: return JSONResponse({"error": "Group not found."}, status_code=404)
    try:
        replacements_data = await fetch_replacements_data()
        target_date_obj = replacements_data.get('date_object')
        if not target_date_obj: return JSONResponse({"error": "Replacements date not available yet."}, status_code=404)

        schedule_data = await get_merged_daily_schedule(target_date_obj, group_name)
        response_data = {"query_group": group_name, "target_date": target_date_obj.isoformat(),
                         "schedule": schedule_data}
        return JSONResponse(response_data)
    except Exception as e:
        logger.error(f"❌ Ошибка API при получении расписания для замен для {group_name}: {e}")
        return JSONResponse({"error": "Internal Server Error", "details": str(e)}, status_code=500)


async def api_schedule_replacements_text_handler(request: Request):
    """API-эндпоинт: Возвращает расписание для группы на дату ЗАМЕН в виде одной строки текста (для KWGT)."""
    group_name = unquote(request.path_params['group_name_encoded']).strip()
    if group_name not in SCHEDULE: return HTMLResponse("Error: Group not found.", status_code=404)
    try:
        replacements_data = await fetch_replacements_data()
        target_date_obj = replacements_data.get('date_object')
        if not target_date_obj: return HTMLResponse("Info: Replacements date not available yet.", status_code=200)

        schedule_data = await get_merged_daily_schedule(target_date_obj, group_name)
        week_type = get_week_type_display(get_week_type())
        text_output = format_schedule_to_kwgt_text(schedule_data, week_type, DEFAULT_SCHEDULE_FORMAT)

        header_date = target_date_obj.strftime('%A, %d.%m')
        text_output = re.sub(r'^.*?\n', f"[c=e74c3c]ЗАМЕНЫ на {header_date}[/c]\n", text_output, 1)

        return HTMLResponse(text_output, media_type="text/plain")
    except Exception as e:
        logger.error(f"❌ Ошибка API KWGT (Replacements): {e}")
        return HTMLResponse("Error: Internal Server Error", status_code=500)


# --- STARLETTE APPLICATION SETUP ---
app_web = Starlette(debug=False, routes=[
    Route('/', endpoint=root_redirect),
    Route('/groups', endpoint=list_groups_handler),
    Route('/schedule/{group_name_encoded:path}', endpoint=show_schedule_handler),

    # Все ваши API маршруты
    Route('/api/replacements_date', endpoint=api_replacements_date_handler),
    Route('/api/schedule_by_date/{group_name_encoded:path}', endpoint=api_schedule_by_date_handler),
    Route('/api/schedule/today_text/{group_name_encoded:path}', endpoint=api_schedule_today_text_handler),
    Route('/api/schedule_for_replacements/{group_name_encoded:path}', endpoint=api_schedule_for_replacements_handler),
    Route('/api/schedule/replacements_text/{group_name_encoded:path}', endpoint=api_schedule_replacements_text_handler),

    # Раздача статических файлов
    Mount('/static', app=StaticFiles(directory='static', check_dir=False), name='static')
])

# --- Точка входа для запуска ---
if __name__ == '__main__':
    load_schedule_data('schedule.json')
    port = int(os.environ.get("PORT", 8000))
    logger.info(f"🚀 Запуск Uvicorn на хосте 0.0.0.0 и порту {port}")
    uvicorn.run(app_web, host="0.0.0.0", port=port)