# price_monitor.py
import os
import json
import time
import logging
import logging.handlers
from datetime import datetime
from typing import Dict, Any

import requests
from telegram.ext import Updater
from dotenv import load_dotenv

# Загружаем переменные окружения
load_dotenv()

class PriceMonitor:
    def __init__(self):
        # Конфигурация
        self.config = {
            'token': os.getenv('TELEGRAM_BOT_TOKEN'),
            'chat_id': os.getenv('TELEGRAM_CHAT_ID'),
            'price_threshold': float(os.getenv('PRICE_CHANGE_THRESHOLD', 5)),
            'mcap_threshold': float(os.getenv('MCAP_CHANGE_THRESHOLD', 5)),
            'check_interval': int(os.getenv('CHECK_INTERVAL', 300)),
            'token_address': '0x96db3e22fdac25c0dff1cab92ae41a697406db7d',
            'network': 'SHAPE_MAINNET',
            'data_file': 'data.json',
        }

        # Настройка логирования
        self.setup_logging()
        
        # Инициализация бота
        self.bot = Updater(token=self.config['token']).bot
        
        self.logger.info("Price monitor initialized with config: %s", 
                        {k: v for k, v in self.config.items() if k != 'token'})

    def setup_logging(self):
        """Настройка системы логирования с ротацией"""
        self.logger = logging.getLogger('PriceMonitor')
        self.logger.setLevel(logging.INFO)

        # Создаем папку для логов если её нет
        os.makedirs('logs', exist_ok=True)

        # Хендлер для ротации логов
        handler = logging.handlers.TimedRotatingFileHandler(
            'logs/price_monitor.log',
            when='midnight',
            interval=1,
            backupCount=14,
            encoding='utf-8'
        )
        
        # Хендлер для вывода в консоль
        console_handler = logging.StreamHandler()
        
        # Форматирование логов
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        handler.setFormatter(formatter)
        console_handler.setFormatter(formatter)
        
        self.logger.addHandler(handler)
        self.logger.addHandler(console_handler)

    def load_data(self) -> Dict[str, Any]:
        """Загрузка последних сохраненных данных"""
        try:
            with open(self.config['data_file'], 'r') as f:
                return json.load(f)
        except FileNotFoundError:
            self.logger.info("No previous data found, starting fresh")
            return {
                'price': 0,
                'market_cap': 0,
                'last_notification_price': 0,
                'last_notification_market_cap': 0
            }

    def save_data(self, data: Dict[str, Any]):
        """Сохранение данных в файл"""
        try:
            with open(self.config['data_file'], 'w') as f:
                json.dump(data, f, indent=2)
            self.logger.info("Data saved successfully")
        except Exception as e:
            self.logger.error("Error saving data: %s", e)

    def fetch_token_data(self) -> Dict[str, Any]:
        """Получение данных о токене через GraphQL API"""
        try:
            response = requests.post(
                "https://zapper.xyz/z/graphql",
                headers={
                    "content-type": "application/json",
                    "apollographql-client-name": "web-relay"
                },
                json={
                    "query": """
                        query TokenPrice($address: Address!, $network: Network!) {
                            fungibleToken(address: $address, network: $network) {
                                symbol
                                name
                                onchainMarketData {
                                    price
                                    marketCap
                                }
                            }
                        }
                    """,
                    "variables": {
                        "address": self.config['token_address'],
                        "network": self.config['network']
                    }
                }
            )
            response.raise_for_status()
            return response.json()['data']['fungibleToken']
        except Exception as e:
            self.logger.error("Error fetching token data: %s", e)
            raise

    def calculate_percentage_change(self, current: float, previous: float) -> float:
        """Расчет процентного изменения"""
        return 0 if previous == 0 else ((current - previous) / previous) * 100

    def send_notification(self, message: str):
        """Отправка уведомления в Telegram"""
        try:
            self.bot.send_message(
                chat_id=self.config['chat_id'],
                text=message,
                parse_mode='HTML'
            )
            self.logger.info("Notification sent: %s", message)
        except Exception as e:
            self.logger.error("Error sending notification: %s", e)

    def check_price_changes(self):
        """Проверка изменений цены и market cap"""
        try:
            previous_data = self.load_data()
            token_data = self.fetch_token_data()
            
            current_price = token_data['onchainMarketData']['price']
            current_market_cap = token_data['onchainMarketData']['marketCap']

            # Расчет изменений от последнего уведомления
            price_change = self.calculate_percentage_change(
                current_price,
                previous_data['last_notification_price'] or current_price
            )
            
            mcap_change = self.calculate_percentage_change(
                current_market_cap,
                previous_data['last_notification_market_cap'] or current_market_cap
            )

            self.logger.info(
                "Current check - Symbol: %s, Price: %s, Market Cap: %s, "
                "Price Change: %s%%, Market Cap Change: %s%%",
                token_data['symbol'], current_price, current_market_cap,
                price_change, mcap_change
            )

            # Проверка необходимости отправки уведомления
            should_notify = False
            notification_message = f"<b>{token_data['name']} ({token_data['symbol']}) Update</b>\n\n"

            if abs(price_change) >= self.config['price_threshold']:
                should_notify = True
                notification_message += f"💰 Price: ${current_price:.8f}\n"
                notification_message += f"📈 Price Change: {price_change:.2f}%\n"

            if abs(mcap_change) >= self.config['mcap_threshold']:
                should_notify = True
                notification_message += f"🏦 Market Cap: ${current_market_cap:.2f}\n"
                notification_message += f"📊 Market Cap Change: {mcap_change:.2f}%\n"

            if should_notify:
                self.send_notification(notification_message)
                # Обновляем точку отсчета
                self.save_data({
                    'price': current_price,
                    'market_cap': current_market_cap,
                    'last_notification_price': current_price,
                    'last_notification_market_cap': current_market_cap,
                    'last_update': datetime.now().isoformat()
                })
            else:
                # Сохраняем текущие значения без обновления точки отсчета
                self.save_data({
                    **previous_data,
                    'price': current_price,
                    'market_cap': current_market_cap,
                    'last_update': datetime.now().isoformat()
                })

        except Exception as e:
            self.logger.error("Error in check cycle: %s", e)
            raise

    def run(self):
        """Основной цикл работы бота"""
        self.logger.info("Starting price monitor...")
        
        while True:
            try:
                self.check_price_changes()
                time.sleep(self.config['check_interval'])
            except Exception as e:
                self.logger.error("Error in main loop: %s", e)
                time.sleep(60)  # Ждем минуту перед повторной попыткой

if __name__ == "__main__":
    monitor = PriceMonitor()
    monitor.run()
