import json
import os
import sys
import time
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

warnings.filterwarnings(
    "ignore",
    message="urllib3 v2 only supports OpenSSL",
)

import requests
from dotenv import load_dotenv
from urllib3.exceptions import InsecureRequestWarning


warnings.simplefilter("ignore", InsecureRequestWarning)


BASE_URL = "https://gorzdrav.spb.ru/service-referral-schedule"
REFERRAL_API_URL = "https://gorzdrav.spb.ru/_api/api/v2/referral/{referral_number}"
STATE_PATH = Path(os.getenv("STATE_FILE", "state.json"))


@dataclass
class CheckResult:
    available: bool
    details: str


CHECK_BUTTON_TEXT = "Проверить талоны"


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    return int(value)


def normalize_referral_number(referral_number: str) -> str:
    return "".join(char for char in referral_number if char.isdigit())


def is_visible(locator) -> bool:
    try:
        return locator.first.is_visible()
    except Exception:
        return False


def first_existing(*locators):
    for locator in locators:
        try:
            if locator.count():
                return locator.first
        except Exception:
            continue
    raise RuntimeError("Не удалось найти подходящий элемент на странице.")


def first_existing_or_none(*locators):
    for locator in locators:
        try:
            if locator.count():
                return locator.first
        except Exception:
            continue
    return None


def page_text(page) -> str:
    try:
        return page.locator("body").inner_text()
    except Exception:
        return ""


def locate_referral_form(page):
    forms = page.locator("form")
    for index in range(forms.count()):
        form = forms.nth(index)
        try:
            text = form.inner_text()
        except Exception:
            continue
        if "Номер направления" in text and "Фамилия" in text:
            return form
    raise RuntimeError("Не удалось найти форму записи по направлению.")


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def get_latest_chat_id(bot_token: str) -> Optional[str]:
    response = requests.get(
        f"https://api.telegram.org/bot{bot_token}/getUpdates",
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        return None

    updates = payload.get("result", [])
    if not updates:
        return None

    for update in reversed(updates):
        message = update.get("message") or update.get("edited_message")
        if not message:
            continue
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is not None:
            return str(chat_id)
    return None


def get_updates(bot_token: str, offset: Optional[int] = None, timeout: int = 0) -> list:
    params = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset

    response = requests.get(
        f"https://api.telegram.org/bot{bot_token}/getUpdates",
        params=params,
        timeout=timeout + 20,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram API error: {payload}")
    return payload.get("result", [])


def send_telegram_message(
    bot_token: str,
    chat_id: str,
    text: str,
    with_keyboard: bool = False,
) -> None:
    reply_markup = None
    if with_keyboard:
        reply_markup = {
            "keyboard": [[{"text": CHECK_BUTTON_TEXT}]],
            "resize_keyboard": True,
        }

    response = requests.post(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": text,
            **({"reply_markup": reply_markup} if reply_markup else {}),
        },
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram API error: {payload}")


def send_bot_menu(bot_token: str, chat_id: str) -> None:
    send_telegram_message(
        bot_token,
        chat_id,
        (
            "Бот следит за талонами автоматически.\n"
            f"Чтобы запустить проверку вручную, нажми кнопку `{CHECK_BUTTON_TEXT}` "
            "или отправь /check."
        ),
        with_keyboard=True,
    )


def fetch_referral_data(referral_number: str, last_name: str) -> dict:
    normalized_referral_number = normalize_referral_number(referral_number)
    response = requests.get(
        REFERRAL_API_URL.format(referral_number=normalized_referral_number),
        params={"lastName": last_name},
        timeout=30,
        verify=False,
    )
    response.raise_for_status()
    payload = response.json()

    if not payload.get("success"):
        message = payload.get("message") or "API gorzdrav вернул неуспешный ответ."
        raise RuntimeError(message)

    result = payload.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("API gorzdrav вернул ответ без поля result.")

    return result


def check_slots_via_api(referral_number: str, last_name: str) -> CheckResult:
    result = fetch_referral_data(referral_number, last_name)
    specialities = result.get("specialities") or []
    if not specialities:
        return CheckResult(
            available=False,
            details="API не вернуло доступных специальностей по этому направлению.",
        )

    slots_found = []
    known_points = []

    for speciality in specialities:
        speciality_name = speciality.get("name") or "Без названия"
        doctors = speciality.get("doctors") or []
        for doctor in doctors:
            doctor_name = doctor.get("name") or "Без имени"
            description = doctor.get("description") or ""
            appointments = doctor.get("appointments") or []

            point_label = " - ".join(part for part in [speciality_name, doctor_name, description] if part)
            if point_label:
                known_points.append(point_label)

            if appointments:
                slots_found.append(
                    {
                        "speciality": speciality_name,
                        "doctor": doctor_name,
                        "description": description,
                        "appointments_count": len(appointments),
                    }
                )

    if slots_found:
        first_slot = slots_found[0]
        details_parts = [
            "API показало доступные талоны.",
            f"Специальность: {first_slot['speciality']}",
        ]
        if first_slot["doctor"] != "Без имени":
            details_parts.append(f"Врач: {first_slot['doctor']}")
        if first_slot["description"]:
            details_parts.append(f"Адрес: {first_slot['description']}")
        details_parts.append(f"Количество найденных слотов: {first_slot['appointments_count']}")
        return CheckResult(
            available=True,
            details="\n".join(details_parts),
        )

    lpu_name = result.get("lpuFullName") or result.get("lpuShortName") or "Медорганизация не указана"
    details = f"Свободных талонов пока нет.\nМедорганизация: {lpu_name}"
    if known_points:
        details += f"\nМаршрут: {known_points[0]}"
    return CheckResult(
        available=False,
        details=details,
    )


def check_slots(referral_number: str, last_name: str, headless: bool) -> CheckResult:
    try:
        return check_slots_via_api(referral_number, last_name)
    except Exception as exc:
        print(f"API-проверка не сработала, переключаюсь на браузерный сценарий: {exc}")

    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError:
        return CheckResult(
            available=False,
            details=(
                "API-проверка не сработала, а браузерный резервный сценарий недоступен, "
                "потому что Playwright не установлен на этом сервере."
            ),
        )

    normalized_referral_number = normalize_referral_number(referral_number)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        page = browser.new_page()
        page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60_000)

        if is_visible(page.get_by_text("временно недоступна", exact=False)):
            browser.close()
            return CheckResult(
                available=False,
                details="Сайт сообщает, что запись временно недоступна из-за регламентных работ.",
            )

        form = locate_referral_form(page)
        referral_input = first_existing(
            form.locator("input[name='referralId']"),
            form.get_by_label("Номер направления"),
            form.get_by_placeholder("Номер направления"),
        )
        surname_input = first_existing(
            form.locator("input[name='lastName']"),
            form.get_by_label("Фамилия"),
            form.get_by_placeholder("Фамилия"),
        )

        referral_input.fill(normalized_referral_number)
        surname_input.fill(last_name)
        referral_input.press("Tab")
        surname_input.press("Tab")

        submit_button = first_existing(
            form.get_by_role("button", name="Продолжить"),
            form.get_by_role("button", name="Далее"),
            form.get_by_role("button", name="Найти"),
            form.locator("button[type='submit']"),
        )
        page.wait_for_function(
            "(button) => !button.disabled",
            arg=submit_button.element_handle(),
            timeout=10_000,
        )
        submit_button.click()

        try:
            page.wait_for_load_state("networkidle", timeout=30_000)
        except PlaywrightTimeoutError:
            pass

        try:
            page.wait_for_function(
                """
                () => {
                    const text = document.body ? document.body.innerText : "";
                    return (
                        text.includes("Отсутствуют свободные талоны") ||
                        text.includes("Идентификатор ошибки") ||
                        text.includes("Номер направления:") ||
                        text.includes("Выбрать талон") ||
                        text.includes("Укажите номер вашего направления")
                    );
                }
                """,
                timeout=15_000,
            )
        except PlaywrightTimeoutError:
            pass

        body_text = page_text(page)

        if "Укажите номер вашего направления" in body_text and "Номер направления:" not in body_text:
            browser.close()
            return CheckResult(
                available=False,
                details="Сайт вернул на первый шаг формы. Похоже, портал временно сработал нестабильно, попробую снова на следующем цикле.",
            )

        choose_ticket = first_existing_or_none(
            page.get_by_role("button", name="Выбрать талон"),
            page.get_by_role("button", name="Выбрать"),
            page.get_by_text("Выбрать талон", exact=False),
        )
        if choose_ticket and is_visible(choose_ticket):
            choose_ticket.click()
            try:
                page.wait_for_load_state("networkidle", timeout=15_000)
            except PlaywrightTimeoutError:
                pass
            body_text = page_text(page)
        else:
            body_text = page_text(page)

        if (
            is_visible(page.get_by_text("Отсутствуют свободные талоны", exact=False))
            or "Отсутствуют свободные талоны" in body_text
        ):
            browser.close()
            return CheckResult(
                available=False,
                details="Свободных талонов пока нет.",
            )

        if is_visible(page.get_by_text("Идентификатор ошибки", exact=False)) or "Идентификатор ошибки" in body_text:
            error_text = body_text or page.locator("body").inner_text()
            browser.close()
            return CheckResult(
                available=False,
                details=f"Сайт вернул ошибку:\n{error_text[:700]}",
            )

        if (
            is_visible(page.get_by_text("Выберите время", exact=False))
            or is_visible(page.get_by_text("Подтверждение записи", exact=False))
            or is_visible(page.get_by_role("button", name="Записаться"))
            or "Выберите время" in body_text
            or "Подтверждение записи" in body_text
        ):
            browser.close()
            return CheckResult(
                available=True,
                details="После выбора специальности сайт показал следующий шаг записи. Свободные талоны, похоже, есть.",
            )

        browser.close()
        return CheckResult(
            available=False,
            details="Не удалось однозначно определить состояние страницы. Проверь разметку сайта вручную.",
        )


def notify_if_needed(result: CheckResult, bot_token: str, chat_id: str, referral_number: str) -> None:
    state = load_state()
    previous_status = state.get("last_status")
    current_status = "available" if result.available else "unavailable"

    should_notify = result.available and previous_status != "available"
    if should_notify:
        send_telegram_message(
            bot_token,
            chat_id,
            (
                "Появились свободные талоны на gorzdrav.spb.ru.\n"
                f"Направление: {referral_number}\n"
                f"Детали: {result.details}"
            ),
        )

    state["last_status"] = current_status
    state["last_details"] = result.details
    state["updated_at"] = int(time.time())
    save_state(state)


def load_offset() -> Optional[int]:
    state = load_state()
    return state.get("telegram_update_offset")


def save_offset(offset: int) -> None:
    state = load_state()
    state["telegram_update_offset"] = offset
    save_state(state)


def perform_check(
    referral_number: str,
    last_name: str,
    bot_token: str,
    chat_id: str,
    headless: bool,
    manual: bool = False,
) -> None:
    result = check_slots(referral_number, last_name, headless=headless)
    print(result.details)
    notify_if_needed(result, bot_token, chat_id, referral_number)

    if manual:
        status = "Есть свободные талоны." if result.available else "Свободных талонов пока нет."
        send_telegram_message(
            bot_token,
            chat_id,
            f"Ручная проверка завершена.\n{status}\n{result.details}",
            with_keyboard=True,
        )


def handle_telegram_updates(
    bot_token: str,
    chat_id: str,
    referral_number: str,
    last_name: str,
    headless: bool,
) -> None:
    offset = load_offset()
    updates = get_updates(bot_token, offset=offset, timeout=0)
    next_offset = offset

    for update in updates:
        update_id = update.get("update_id")
        if update_id is not None:
            next_offset = update_id + 1

        message = update.get("message") or update.get("edited_message")
        if not message:
            continue

        current_chat_id = str((message.get("chat") or {}).get("id", ""))
        if current_chat_id != chat_id:
            continue

        text = (message.get("text") or "").strip()
        if text in {"/start", "/menu"}:
            send_bot_menu(bot_token, chat_id)
        elif text in {"/check", CHECK_BUTTON_TEXT}:
            send_telegram_message(
                bot_token,
                chat_id,
                "Запускаю ручную проверку, это может занять до минуты.",
                with_keyboard=True,
            )
            perform_check(
                referral_number,
                last_name,
                bot_token,
                chat_id,
                headless,
                manual=True,
            )

    if next_offset is not None:
        save_offset(next_offset)


def main() -> int:
    load_dotenv()

    referral_number = os.getenv("REFERRAL_NUMBER", "").strip()
    last_name = os.getenv("LAST_NAME", "").strip()
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    headless = env_bool("HEADLESS", True)
    interval_minutes = env_int("CHECK_INTERVAL_MINUTES", 10)

    if not referral_number or not last_name or not bot_token:
        print("Нужно заполнить REFERRAL_NUMBER, LAST_NAME и TELEGRAM_BOT_TOKEN в .env")
        return 1

    if not chat_id:
        chat_id = get_latest_chat_id(bot_token) or ""
        if chat_id:
            print(f"Найден chat_id: {chat_id}")
        else:
            print("Не удалось найти TELEGRAM_CHAT_ID. Напиши что-нибудь своему боту в Telegram и запусти снова.")
            return 1

    run_once = "--once" in sys.argv
    next_check_at = 0.0

    if not run_once:
        send_bot_menu(bot_token, chat_id)

    while True:
        try:
            if run_once or time.time() >= next_check_at:
                perform_check(
                    referral_number,
                    last_name,
                    bot_token,
                    chat_id,
                    headless,
                    manual=False,
                )
                next_check_at = time.time() + interval_minutes * 60

            if run_once:
                return 0

            handle_telegram_updates(
                bot_token,
                chat_id,
                referral_number,
                last_name,
                headless,
            )
        except KeyboardInterrupt:
            print("Остановлено пользователем.")
            return 0
        except Exception as exc:
            print(f"Ошибка проверки: {exc}")
            time.sleep(5)
            continue

        time.sleep(5)


if __name__ == "__main__":
    raise SystemExit(main())
