#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Telegram menu navigation."""

from __future__ import annotations

import asyncio
import datetime
import imghdr
import logging
import mimetypes
from pathlib import Path
from typing import Any, List, Optional, Type, Union

import telegram.ext
import validators
from apscheduler.schedulers.base import BaseScheduler
from telegram import Bot, Chat, InlineKeyboardMarkup, Message, ReplyKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, PicklePersistence
from telegram.ext._callbackcontext import CallbackContext

from ._version import __raw_url__
from .models import BaseMessage, ButtonType, TypeCallback, emoji_replace

logger = logging.getLogger(__name__)


class NavigationException(Exception):
    """Base exception."""


class TelegramMenuSession:
    """Session manager, send start message to each new user connecting to the bot."""

    # delays in seconds
    READ_TIMEOUT = 6
    CONNECT_TIMEOUT = 7
    START_MESSAGE = "start"

    def __init__(self, api_key: str, start_message: str = START_MESSAGE) -> None:
        """
        Initialize the session object.

        Parameters
        ----------
        api_key: Telegram bot API key
        start_message: text used to start a session, e.g. /start

        """
        if not isinstance(api_key, str):
            raise KeyError("API_KEY must be a string!")

        persistence = PicklePersistence(filepath="arbitrarycallbackdatabot")
        self.application = (
            Application.builder().token(api_key).persistence(persistence).arbitrary_callback_data(True).build()
        )
        self.scheduler = self.application.job_queue.scheduler  # type: ignore

        self._api_key = api_key
        self.sessions: List[NavigationHandler] = []
        self.start_message_class: Optional[Type[BaseMessage]] = None
        self.start_message_args: Optional[List[Any]] = None
        self.navigation_handler_class: Optional[Type[NavigationHandler]] = None

        # on different commands - answer in Telegram
        self.application.add_handler(CommandHandler(start_message, self._send_start_message))
        self.application.add_handler(MessageHandler(telegram.ext.filters.TEXT, self._button_select_callback))
        self.application.add_handler(
            MessageHandler(telegram.ext.filters.StatusUpdate.WEB_APP_DATA, self._button_webapp_callback)
        )
        self.application.add_handler(CallbackQueryHandler(self._button_inline_select_callback))
        self.application.add_handler(telegram.ext.PollAnswerHandler(self._poll_answer))
        self.application.add_error_handler(self._msg_error_handler)

    def start(
        self,
        start_message_class: Type[BaseMessage],
        start_message_args: Optional[List[Any]] = None,
        polling: bool = True,
        navigation_handler_class: Optional[Type[NavigationHandler]] = None,
    ) -> None:
        """
        Set the start message and run the dispatcher.

        Parameters
        ----------
        start_message_class: class used to create start message
        start_message_args: optional arguments passed to the start class
        polling: if True, start polling updates from Telegram
        navigation_handler_class: optional class used to extend the base NavigationHandler

        """
        self.start_message_class = start_message_class
        self.start_message_args = start_message_args
        self.navigation_handler_class = navigation_handler_class or NavigationHandler
        if not issubclass(start_message_class, BaseMessage):
            raise NavigationException("start_message_class must be a BaseMessage!")
        if start_message_args is not None and not isinstance(start_message_args, list):
            raise NavigationException("start_message_args is not a list!")
        if not issubclass(self.navigation_handler_class, NavigationHandler):
            raise NavigationException("navigation_handler_class must be a NavigationHandler!")

        if not self.scheduler.running:
            self.scheduler.start()
        if polling:
            self.application.run_polling()

    async def _send_start_message(self, update: Update, _: Any) -> None:  # type: ignore
        """Start main message, app choice."""
        chat = update.effective_chat
        if chat is None:
            raise NavigationException("Chat object was not created")
        if self.navigation_handler_class is None:
            raise NavigationException("Navigation Handler class not defined")
        session = self.navigation_handler_class(self.application.bot, chat, self.scheduler)
        self.sessions.append(session)
        if self.start_message_class is None:
            raise NavigationException("Message class not defined")
        if self.start_message_args is not None:
            start_message = self.start_message_class(session, message_args=self.start_message_args)
        else:
            start_message = self.start_message_class(session)
        await session.goto_menu(start_message)

    def get_session(self, chat_id: int = 0) -> Optional[NavigationHandler]:
        """Get session from list."""
        sessions = [x for x in self.sessions if chat_id in (x.chat_id, 0)]
        if not sessions:
            return None
        return sessions[0]

    async def _button_select_callback(self, update: Update, context: CallbackContext) -> None:  # type: ignore
        """Menu message main entry point."""
        if update.effective_chat is None:
            raise NavigationException("Chat object was not created")
        session = self.get_session(update.effective_chat.id)
        if session is None:
            await self._send_start_message(update, context)
            return
        await session.select_menu_button(update.message.text)

    async def _poll_answer(self, update: Update, _: CallbackContext) -> None:  # type: ignore
        """Entry point for poll selection."""
        if update.effective_user is None:
            raise NavigationException("User object was not created")
        session = next((x for x in self.sessions if x.user_name == update.effective_user.first_name), None)
        if session:
            await session.poll_answer(update.poll_answer.option_ids[0])

    async def _button_inline_select_callback(self, update: Update, context: CallbackContext) -> None:  # type: ignore
        """Execute inline callback of an BaseMessage."""
        if update.effective_chat is None:
            raise NavigationException("Chat object was not created")
        session = self.get_session(update.effective_chat.id)
        if session is None:
            await self._send_start_message(update, context)
            return
        await session.app_message_button_callback(update.callback_query.data, update.callback_query.id)

    async def _button_webapp_callback(self, update: Update, context: CallbackContext) -> None:  # type: ignore
        """Execute webapp callback."""
        if (
            update.effective_chat is None
            or update.effective_message is None
            or update.effective_message.web_app_data is None
        ):
            raise NavigationException("Chat object was not created")
        session = self.get_session(update.effective_chat.id)
        if session is None:
            await self._send_start_message(update, context)
            return
        await session.app_message_webapp_callback(
            update.effective_message.web_app_data.data, update.effective_message.web_app_data.button_text
        )

    @staticmethod
    async def _msg_error_handler(update: object, context: CallbackContext) -> None:  # type: ignore
        """Log Errors caused by Updates."""
        if not isinstance(update, Update):
            raise NavigationException("Incorrect update object")
        error_message = str(context.error) if update is None else f"Update {update.update_id} - {str(context.error)}"
        logger.error(error_message)

    async def broadcast_message(self, message: str, notification: bool = True) -> List[telegram.Message]:
        """Broadcast simple message without keyboard markup to all sessions."""
        messages = []
        for session in self.sessions:
            msg = await session.send_message(message, notification=notification)
            if msg is not None:
                messages.append(msg)
        return messages

    async def broadcast_picture(self, picture_path: str, notification: bool = True) -> List[telegram.Message]:
        """Broadcast picture to all sessions."""
        messages = []
        for session in self.sessions:
            msg = await session.send_photo(picture_path, notification=notification)
            if msg is not None:
                messages.append(msg)
        return messages

    async def broadcast_sticker(self, sticker_path: str, notification: bool = True) -> List[telegram.Message]:
        """Broadcast picture to all sessions."""
        messages = []
        for session in self.sessions:
            msg = await session.send_sticker(sticker_path, notification=notification)
            if msg is not None:
                messages.append(msg)
        return messages


class NavigationHandler:
    """Navigation handler for Telegram application."""

    POLL_DEADLINE = 10  # seconds
    MESSAGE_CHECK_TIMEOUT = 10  # seconds
    CONNECTION_POOL_SIZE = 8

    def __init__(self, bot: Bot, chat: Chat, scheduler: BaseScheduler) -> None:
        """Init NavigationHandler class."""
        self._bot = bot
        self._poll: Optional[Message] = None
        self._poll_callback: Optional[TypeCallback] = None

        self.scheduler = scheduler
        self.chat_id = chat.id
        self.user_name = chat.first_name
        self.poll_name = f"poll_{self.user_name}"

        logger.info(f"Opening chat with user {self.user_name}")

        self._menu_queue: List[BaseMessage] = []  # list of menus selected by user
        self._message_queue: List[BaseMessage] = []  # list of application messages sent

        # check if messages have expired every MESSAGE_CHECK_TIMEOUT seconds
        scheduler.add_job(
            self._expiry_date_checker,
            "interval",
            id="state_nav_update",
            seconds=self.MESSAGE_CHECK_TIMEOUT,
            replace_existing=True,
        )

    async def _expiry_date_checker(self) -> None:
        """Check expiry date of message and delete if expired."""
        for message in self._message_queue:
            if message.has_expired():
                await self._delete_queued_message(message)

        # go back to home after sub-menu message has expired
        if len(self._menu_queue) >= 2 and self._menu_queue[-1].has_expired():
            await self.goto_home()

    async def delete_message(self, message_id: int) -> None:
        """Delete a message from its id."""
        await self._bot.delete_message(chat_id=self.chat_id, message_id=message_id)

    async def _delete_queued_message(self, message: BaseMessage) -> None:
        """Delete a message, remove from queue."""
        message.kill_message()
        if message in self._message_queue:
            self._message_queue.remove(message)
            await self.delete_message(message.message_id)
        del message

    async def goto_menu(self, menu_message: BaseMessage) -> int:
        """Send menu message and add to queue."""
        content = menu_message.update()
        logger.info(f"Opening menu {menu_message.label}")
        keyboard = menu_message.gen_keyboard_content()
        message = await self.send_message(emoji_replace(content), keyboard, notification=menu_message.notification)
        menu_message.is_alive()
        self._menu_queue.append(menu_message)
        return message.message_id

    async def goto_home(self) -> int:
        """Go to home menu, empty menu_queue."""
        if len(self._menu_queue) == 1:
            # already at 'home' level
            return self._menu_queue[0].message_id
        menu_previous = self._menu_queue.pop()
        while self._menu_queue:
            menu_previous = self._menu_queue.pop()
        return await self.goto_menu(menu_previous)

    @staticmethod
    def filter_unicode(input_string: str) -> str:
        """Remove non-unicode characters from input string."""
        return input_string.encode("ascii", "ignore").decode("utf-8")

    async def _send_app_message(self, message: BaseMessage, label: str) -> int:
        """Send an application message."""
        content = emoji_replace(message.update())
        # if message with this label already exist in message_queue, delete it and replace it
        info_message = self.filter_unicode(f"Send message '{message.label}': '{label}'")
        logger.info(str(info_message))
        if "_" not in message.label:
            message.label = f"{message.label}_{label}"

        # delete message if already displayed
        message_existing = self.get_message(message.label)
        if message_existing is not None:
            await self._delete_queued_message(message)

        message.is_alive()

        keyboard = message.gen_inline_keyboard_content()
        msg = await self.send_message(content, keyboard, message.notification)
        message.message_id = msg.message_id
        self._message_queue.append(message)

        message.content_previous = content
        message.keyboard_previous = message.keyboard.copy()
        return message.message_id

    async def send_message(
        self,
        content: str,
        keyboard: Optional[Union[ReplyKeyboardMarkup, InlineKeyboardMarkup]] = None,
        notification: bool = True,
    ) -> telegram.Message:
        """Send a text message with html formatting."""
        return await self._bot.send_message(
            chat_id=self.chat_id,
            text=content,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_notification=not notification,
        )

    async def edit_message(self, message: BaseMessage) -> bool:
        """Edit an inline message asynchronously."""
        message_updt = self.get_message(message.label)
        if message_updt is None:
            return False

        # check if content and keyboard have changed since previous message
        content = emoji_replace(message_updt.update())
        if not self._message_check_changes(message_updt, content):
            return False

        keyboard_format = message_updt.gen_inline_keyboard_content()
        try:
            await self._bot.edit_message_text(
                text=content,
                chat_id=self.chat_id,
                message_id=message_updt.message_id,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard_format,
            )
        except telegram.error.BadRequest as error:
            logger.error(error)
            return False
        return True

    @staticmethod
    def _message_check_changes(message: BaseMessage, content: str) -> bool:
        """Check is message content and keyboard has changed since last edit."""
        content_identical = content == message.content_previous
        keyboard_identical = [y.label for x in message.keyboard_previous for y in x] == [
            y.label for x in message.keyboard for y in x
        ]
        if content_identical and keyboard_identical:
            return False
        message.content_previous = content
        message.keyboard_previous = message.keyboard.copy()
        return True

    async def select_menu_button(self, label: str) -> Optional[int]:  # noqa: C901
        """Select menu button using label."""
        msg_id = 0
        if label == "Back":
            if len(self._menu_queue) == 1:
                # already at 'home' level
                return self._menu_queue[0].message_id
            menu_previous = self._menu_queue.pop()  # delete actual menu
            if self._menu_queue:
                menu_previous = self._menu_queue.pop()
            return await self.goto_menu(menu_previous)
        if label == "Home":
            return await self.goto_home()

        for menu_item in self._menu_queue[::-1]:
            btn = menu_item.get_button(label)
            if not btn:
                continue
            if isinstance(btn.callback, BaseMessage):
                if btn.callback.inlined:
                    msg_id = await self._send_app_message(btn.callback, label)
                    if btn.callback.home_after:
                        msg_id = await self.goto_home()
                else:
                    msg_id = await self.goto_menu(btn.callback)
            elif btn.callback is not None and hasattr(btn.callback, "__call__"):
                if asyncio.iscoroutinefunction(btn.callback):
                    await btn.callback()  # type: ignore
                else:
                    btn.callback()  # type: ignore
            return msg_id

        # label does not match any sub-menu, just process the user input
        await self.capture_user_input(label)
        return None

    async def capture_user_input(self, label: str) -> None:
        """Process the user input in the last message updated."""
        last_menu_message = self._menu_queue[-1]
        if self._message_queue:
            last_app_message = self._message_queue[-1]
            if last_app_message.time_alive > last_menu_message.time_alive:
                last_menu_message = last_app_message
        await last_menu_message.text_input(label)

    async def app_message_webapp_callback(self, webapp_data: str, button_text: str) -> None:
        """Execute the callback associated to this webapp."""
        last_menu = self._menu_queue[-1]
        webapp_message = next(iter(y for x in last_menu.keyboard for y in x if y.label == button_text), None)
        if webapp_message is not None and callable(webapp_message.callback):
            if asyncio.iscoroutinefunction(webapp_message.callback):
                html_response = await webapp_message.callback(webapp_data)
            else:
                html_response = webapp_message.callback(webapp_data)
            await self.send_message(html_response, notification=webapp_message.notification)

    async def app_message_button_callback(self, callback_label: str, callback_id: str) -> None:
        """Entry point to execute an action after message button selection."""
        label_message, label_action = callback_label.split(".")
        log_message = self.filter_unicode(f"Received action request from '{label_message}': '{label_action}'")
        logger.info(log_message)
        message = self.get_message(label_message)
        if message is None:
            logger.error(f"Message with label {label_message} not found, return")
            return
        btn = message.get_button(label_action)

        if btn is None:
            logger.error(f"No button found with label {label_action}, return")
            return

        if btn.btype in [ButtonType.PICTURE, ButtonType.STICKER]:
            # noinspection PyTypeChecker
            await self._bot.send_chat_action(chat_id=self.chat_id, action=ChatAction.UPLOAD_PHOTO)
        elif btn.btype == ButtonType.MESSAGE:
            # noinspection PyTypeChecker
            await self._bot.send_chat_action(chat_id=self.chat_id, action=ChatAction.TYPING)
        elif btn.btype == ButtonType.POLL:
            await self.send_poll(question=btn.args[0], options=btn.args[1])
            self._poll_callback = btn.callback
            await self._bot.answer_callback_query(callback_id, text="Select an answer...")
            return

        if not callable(btn.callback):
            return

        if btn.args is not None:
            if asyncio.iscoroutinefunction(btn.callback):
                action_status = await btn.callback(btn.args)
            else:
                action_status = btn.callback(btn.args)
        elif asyncio.iscoroutinefunction(btn.callback):
            action_status = await btn.callback()
        else:
            action_status = btn.callback()

        # send picture if custom label found
        if btn.btype == ButtonType.PICTURE:
            await self.send_photo(picture_path=action_status, notification=btn.notification)
            await self._bot.answer_callback_query(callback_id, text="Picture sent!")
            return
        if btn.btype == ButtonType.STICKER:
            await self.send_sticker(sticker_path=action_status, notification=btn.notification)
            await self._bot.answer_callback_query(callback_id, text="Sticker sent!")
            return
        if btn.btype == ButtonType.MESSAGE:
            await self.send_message(action_status, notification=btn.notification)
            await self._bot.answer_callback_query(callback_id, text="Message sent!")
            return
        await self._bot.answer_callback_query(callback_id, text=action_status)

        # update expiry period and update
        message.is_alive()
        await self.edit_message(message)

    @staticmethod
    def _sticker_check_replace(sticker_path: str) -> Union[str, bytes]:
        """Check if sticker is correct file type."""
        try:
            if not sticker_path.lower().endswith(".webp"):
                raise ValueError("Sticker has no .webp format")
            if validators.url(sticker_path):
                # todo: add check if url exists
                return sticker_path
            if Path(sticker_path).is_file() and imghdr.what(sticker_path):
                with open(sticker_path, "rb") as file_h:
                    return file_h.read()
            raise ValueError("Path is not a picture")
        except ValueError:
            url_default = f"{__raw_url__}/resources/stats_default.webp"
            logger.error(f"Picture path '{sticker_path}' is invalid, replacing with default {url_default}")
            return url_default

    @staticmethod
    def _picture_check_replace(picture_path: str) -> Union[str, bytes]:
        """Check if the given picture path or uri is correct, replace by default if not."""
        try:
            if validators.url(picture_path):
                # check if the url has image format
                mimetype, _ = mimetypes.guess_type(picture_path)
                if mimetype and mimetype.startswith("image"):
                    return picture_path
                raise ValueError("Url is not a picture")
            if Path(picture_path).is_file() and imghdr.what(picture_path):
                with open(picture_path, "rb") as file_h:
                    return file_h.read()
            raise ValueError("Path is not a picture")
        except ValueError:
            url_default = f"{__raw_url__}/resources/stats_default.png"
            logger.error(f"Picture path '{picture_path}' is invalid, replacing with default {url_default}")
            return url_default

    async def send_photo(self, picture_path: str, notification: bool = True) -> Optional[telegram.Message]:
        """Send a picture."""
        picture_obj = self._picture_check_replace(picture_path=picture_path)
        try:
            return await self._bot.send_photo(
                chat_id=self.chat_id, photo=picture_obj, disable_notification=not notification
            )
        except telegram.error.BadRequest as error:
            logger.error(f"Failed to send picture {picture_path}: {error}")
        return None

    async def send_sticker(self, sticker_path: str, notification: bool = True) -> Optional[telegram.Message]:
        """Send a picture."""
        sticker_obj = self._sticker_check_replace(sticker_path=sticker_path)
        try:
            return await self._bot.send_sticker(
                chat_id=self.chat_id, sticker=sticker_obj, disable_notification=not notification
            )
        except telegram.error.BadRequest as error:
            logger.error(f"Failed to send picture {sticker_path}: {error}")
        return None

    def get_message(self, label_message: str) -> Optional[BaseMessage]:
        """Get message from message_queue matching attribute label_message."""
        return next(iter(x for x in self._message_queue if x.label == label_message), None)

    async def send_poll(self, question: str, options: List[str]) -> None:
        """Send poll to user with question and options."""
        if self.scheduler.get_job(self.poll_name) is not None:
            await self.poll_delete()
        options = [emoji_replace(x) for x in options]
        self._poll = await self._bot.send_poll(
            chat_id=self.chat_id,
            question=emoji_replace(question),
            options=options,
            is_anonymous=False,
            open_period=self.POLL_DEADLINE,
        )
        self.scheduler.add_job(
            self.poll_delete,
            "date",
            id=self.poll_name,
            next_run_time=datetime.datetime.now() + datetime.timedelta(seconds=self.POLL_DEADLINE + 1),
            replace_existing=True,
        )

    async def poll_delete(self) -> None:
        """Run when poll timeout has expired."""
        if self._poll is not None:
            try:
                logger.info(f"Deleting poll '{self._poll.poll.question}'")
                await self._bot.delete_message(chat_id=self.chat_id, message_id=self._poll.message_id)
            except telegram.error.BadRequest:
                logger.error(f"Poll message {self._poll.message_id} already deleted")

    async def poll_answer(self, answer_id: int) -> None:
        """Run when poll message is received."""
        if self._poll is None or self._poll_callback is None or not callable(self._poll_callback):
            logger.error("Poll is not defined")
            return

        answer_ascii = self._poll.poll.options[answer_id].text.encode("ascii", "ignore").decode()
        logger.info(f"{self.user_name}'s answer to question '{self._poll.poll.question}' is '{answer_ascii}'")
        if asyncio.iscoroutinefunction(self._poll_callback):
            await self._poll_callback(self._poll.poll.options[answer_id].text)
        else:
            self._poll_callback(self._poll.poll.options[answer_id].text)
        await asyncio.sleep(1)
        await self.poll_delete()

        if self.scheduler.get_job(self.poll_name) is not None:
            self.scheduler.remove_job(self.poll_name)
        self._poll = None
