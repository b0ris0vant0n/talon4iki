from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot


def test_normalize_status_text_for_unavailable_result():
    result = bot.CheckResult(available=False, details="Свободных талонов пока нет.")

    assert bot.normalize_status_text(result) == "❌ Свободных талонов пока нет."


def test_normalize_status_text_for_available_result_without_duplicate_prefix():
    result = bot.CheckResult(available=True, details="✅ Есть свободные талоны.")

    assert bot.normalize_status_text(result) == "✅ Есть свободные талоны."


def test_check_slots_via_api_returns_available_when_appointments_found(monkeypatch):
    monkeypatch.setattr(
        bot,
        "fetch_referral_data",
        lambda referral_number, last_name: {
            "lpuFullName": 'СПб ГБУЗ "Городская Мариинская больница" Амбулаторно-консультативное отделение',
            "specialities": [
                {
                    "name": "Кабинет",
                    "doctors": [
                        {
                            "name": None,
                            "description": "191014, Санкт-Петербург, Литейный пр., д.56",
                            "appointments": [{"id": "slot-1"}, {"id": "slot-2"}],
                        }
                    ],
                }
            ],
        },
    )

    result = bot.check_slots_via_api("78264737846700", "Елисеева")

    assert result.available is True
    assert "API показало доступные талоны." in result.details
    assert "Количество найденных слотов: 2" in result.details


def test_check_slots_via_api_returns_unavailable_when_no_appointments(monkeypatch):
    monkeypatch.setattr(
        bot,
        "fetch_referral_data",
        lambda referral_number, last_name: {
            "lpuFullName": 'СПб ГБУЗ "Городская Мариинская больница" Амбулаторно-консультативное отделение',
            "specialities": [
                {
                    "name": "Кабинет",
                    "doctors": [
                        {
                            "name": None,
                            "description": "191014, Санкт-Петербург, Литейный пр., д.56",
                            "appointments": [],
                        }
                    ],
                }
            ],
        },
    )

    result = bot.check_slots_via_api("78264737846700", "Елисеева")

    assert result.available is False
    assert "Свободных талонов пока нет." in result.details
    assert "Маршрут: Кабинет - Без имени - 191014, Санкт-Петербург, Литейный пр., д.56" in result.details


def test_notify_if_needed_sends_alert_and_booking_prompt_for_new_available_slot(monkeypatch):
    sent_messages = []
    booking_prompts = []
    saved_state = {}

    monkeypatch.setattr(bot, "load_state", lambda: {})
    monkeypatch.setattr(bot, "save_state", lambda state: saved_state.update(state))
    monkeypatch.setattr(
        bot,
        "send_telegram_message",
        lambda bot_token, chat_id, text, with_keyboard=False, reply_markup=None: sent_messages.append(text),
    )
    monkeypatch.setattr(
        bot,
        "send_booking_prompt",
        lambda bot_token, chat_id: booking_prompts.append((bot_token, chat_id)),
    )

    bot.notify_if_needed(
        result=bot.CheckResult(available=True, details="Тест: найден талон."),
        bot_token="token",
        chat_id="chat",
        referral_number="78264737846700",
        last_name="Елисеева",
        notify_on_every_check=False,
    )

    assert len(sent_messages) == 1
    assert sent_messages[0].startswith("✅ Появились свободные талоны")
    assert booking_prompts == [("token", "chat")]
    assert saved_state["last_status"] == "available"


def test_handle_telegram_updates_processes_booking_yes_callback(monkeypatch):
    callback_answers = []
    sent_booking_links = []
    saved_offsets = []

    monkeypatch.setattr(
        bot,
        "load_offset",
        lambda: 100,
    )
    monkeypatch.setattr(
        bot,
        "get_updates",
        lambda bot_token, offset=None, timeout=0: [
            {
                "update_id": 101,
                "callback_query": {
                    "id": "cbq-1",
                    "data": bot.BOOK_APPOINTMENT_YES,
                    "message": {"chat": {"id": 138554631}},
                },
            }
        ],
    )
    monkeypatch.setattr(bot, "save_offset", lambda offset: saved_offsets.append(offset))
    monkeypatch.setattr(
        bot,
        "answer_callback_query",
        lambda bot_token, callback_query_id, text="": callback_answers.append((callback_query_id, text)),
    )
    monkeypatch.setattr(
        bot,
        "send_booking_link",
        lambda bot_token, chat_id, referral_number, last_name: sent_booking_links.append(
            (chat_id, referral_number, last_name)
        ),
    )

    bot.handle_telegram_updates(
        bot_token="token",
        chat_id="138554631",
        referral_number="78264737846700",
        last_name="Елисеева",
        headless=True,
        notify_on_every_check=False,
    )

    assert callback_answers == [("cbq-1", "Открываю данные для записи")]
    assert sent_booking_links == [("138554631", "78264737846700", "Елисеева")]
    assert saved_offsets == [102]
