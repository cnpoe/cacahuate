import logging
import pika
import traceback

from .handler import Handler

LOGGER = logging.getLogger(__name__)


class Loop:

    def __init__(self, config: dict):
        self.config = config
        self.handler = Handler(config)

    def start(self):
        connection = pika.BlockingConnection(pika.ConnectionParameters(
            host=self.config['RABBIT_HOST'],
        ))
        channel = connection.channel()

        channel.queue_declare(
            queue=self.config['RABBIT_QUEUE'],
            durable=True,
        )

        channel.basic_consume(
            self.handler,
            queue=self.config['RABBIT_QUEUE'],
            consumer_tag=self.config['RABBIT_CONSUMER_TAG'],
            no_ack=self.config['RABBIT_NO_ACK'],
        )

        LOGGER.info('cacahuate started')

        try:
            channel.start_consuming()
        except KeyboardInterrupt:
            LOGGER.info('cacahuate stopped')
        except Exception as e:
            LOGGER.error(traceback.format_exc())
