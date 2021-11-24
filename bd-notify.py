#!/usr/bin/env python3
import requests
import yaml
import argparse
import datetime
import json
import boto3
import sys

from botocore.exceptions import ClientError
from apscheduler.schedulers.blocking import BlockingScheduler
from playsound import playsound
from plyer import notification


SUCTION_CUP = {False: 22, True: 23}
CUMTUBE = {False: 18, True: 13}
FIRMNESS = {"soft": [7], "extrasoft": [5], "medium": [4], "firm": [9], "split": [121, 12, 123, 11, 124]}
SIZES = {"onesize": 6, "mini": 10, "small": 1, "medium": 2, "large": 8, "extralarge": 3, "2xlarge": 287}

CONFIG_PATH = "./bd-notify-config.yml"
BASE_URL = "https://bad-dragon.com/api/inventory-toys"
SKU_URL = "https://bad-dragon.com/api/products"

CATEGORIES = {
    "insertable": "dildo",
    "penetrable": "masturbator",
    "Packer": "packer",
    "vibrator": "lil' vibe",
    "shooter": "lil' squirt",
    "wearables": "wearable",
}


def main():
    parser = argparse.ArgumentParser(
        description="Automatically send a push notification upon a certain bad dragon toy coming in stock.",
        usage="bd-notifier [sku] [options]")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "sku", nargs="*", default="+",
        help="A list of SKUs (product names) to look for. Use + or leave blank"
             " to indicate any SKU. Indicating a category is disallowed if one or"
             " more SKUs are specified."
    )

    parser.add_argument("--config", default="default", type=str, help="Specify a configuration to use.")

    for t in CATEGORIES.keys():
        group.add_argument(f"-{t[0]}", f"--{t.lower()}", action="store_true", help=f"Include exclusively {CATEGORIES[t].lower()} category toys. Only one category may be specified at a time.")

    parser.add_argument("--size", action="extend", nargs="+", type=str, help="To narrow down your search to one or more specific sizes, use this argument."
                                                                             " Valid sizes are onesize, mini, small, medium, large, extralarge, and 2xlarge.")
    parser.add_argument("--firmness", action="extend", nargs="+", type=str, help="To narrow down your search to one or more specific firmnesses, use this argument."
                                                                                 " Valid firmnesses are extrasoft, soft, medium, firm, and split.")

    parser.add_argument("-m", "--max-price", type=float, default=300, help="To set a maximum price, use the -m argument with the price of your choice."
                                                                           " Must be less than or equal to 300 (the default).")

    parser.add_argument("-t", "--cumtube", action="store_true", help="Exclude items without cumtubes")
    parser.add_argument("-c", "--suction-cup", action="store_true", help="Exclude items without a suction cup")
    parser.add_argument("-n", "--no-features", action="store_true", help="Exclude any items with additional features")

    parser.add_argument("-r", "--ready-made", action="store_true", help="Include only ready-made items")
    parser.add_argument("-f", "--flops", action="store_true", help="Include only flops")

    parser.add_argument("-V", "--verbose", action="store_true", help="Display debug output")

    parsed = vars(parser.parse_args())

    if parsed["verbose"]:
        print("------ Arguments ------")
        print(parsed)

    if parsed["no_features"] and (parsed["cumtube"] or parsed["suction_cup"]):
        print("error: Cannot specify no features when features are specified")
        return

    if parsed["ready_made"] and parsed["flops"]:
        print("error: Cannot specify exclusively ready-made and exclusively flops at the same time.")
        print("If you wish to include both, omit both ready-made and flop options to look for all options.")
        return

    if parsed["max_price"] > 300:
        print("error: Max price cannot be greater than 300.")
        return

    # Prevent weird floating string-floating point conversion errors
    if parsed["max_price"] == 300:
        parsed["max_price"] = 300

    config = load_config(CONFIG_PATH, parsed["config"])

    if parsed["verbose"]:
        print("------ Config ------")
        print(config)

    notifier = BDNotify(parsed, config)
    notifier.start_fetch_loop()


def load_config(path, config_profile):
    with open(path, "r") as f:
        content = f.read()

    config = yaml.safe_load(content)
    return config[config_profile]

class BDNotify(object):
    def __init__(self, args, config, time=datetime):
        self.args = args
        self.config = config
        self.time = time
        self.notified_ids = set()

        if self.args["verbose"]:
            print("---- BDNotify Args ----")
            print(self.args)
            print("---- BDNotify Config ----")
            print(self.config)

        self.setup_parameters()

    def setup_parameters(self):
        self.parameters = {
            "sort[field]": "price",
            "sort[direction]": "asc",
            "page": 1,
            "limit": 60,
            "price[min]": 0,
            "price[max]": self.args["max_price"]
        }

        if self.args["size"] is not None:
            self.parameters["sizes[]"] = []

            for size in self.args["size"]:
                self.parameters["sizes[]"].append(SIZES[size])

        if self.args["firmness"] is not None:
            self.parameters["firmnessValues[]"] = []

            for firmness in self.args["firmness"]:
                self.parameters["firmnessValues[]"].extend(FIRMNESS[firmness])

        if self.args["sku"] is not None:
            self.parameters["skus[]"] = []

            for sku in self.args["sku"]:
                self.parameters["skus[]"].append(sku)

        if self.args["ready_made"]:
            self.parameters["type[]"] = ["ready_made"]
        elif self.args["flops"]:
            self.parameters["type[]"] = ["flop"]

        selected_category = self.get_category()

        if selected_category is not None:
            self.parameters["category"] = selected_category

        if self.args["cumtube"]:
            self.parameters["cumtube"] = CUMTUBE[True]

        if self.args["suction_cup"]:
            self.parameters["suctionCup"] = SUCTION_CUP[True]

        if self.args["no_features"]:
            self.parameters["noAccessories"] = 1

    def start_fetch_loop(self):
        # Initial tjhiogaer
        self.notify_loop()

        scheduler = BlockingScheduler()

        scheduler.add_job(self.notify_loop, "interval", seconds=self.config["check-time"])

        if self.args["verbose"]:
            print("Starting fetch loop...")

        scheduler.start()

    def notify_loop(self):
        if self.args["verbose"]:
            print("Fetching things...")

        toy = self.fetch_toy_properties()

        if self.args["verbose"]:
            print(toy)

        if toy is None:
            return

        if self.config["notify"]:
            self.send_configured_notification(**toy)

        if self.config["audio"]:
            self.play_configured_sound()

        if "sns-publish" in self.config and self.config["sns-publish"]:
            self.push_sns_notification(**toy)


    # Returns None if no toys of the matching description are found, returns a dict of
    # properties about a single one of the toys found otherwise (the first is normally used)
    def fetch_toy_properties(self):
        response = json.loads(requests.get(BASE_URL, params=self.parameters).text)

        if self.args["verbose"]:
            print(response)

        if "toys" not in response:
            return None

        toys = response["toys"]

        if len(toys) == 0:
            return None
        else:
            toy = toys[0]

        # Get the sku of the toy, then send a request to https://bad-dragon.com/api/products/[sku] to get the type.
        toy_name = toy["sku"]

        response = json.loads(requests.get(SKU_URL + "/" + toy_name).text)

        if self.args["verbose"]:
            print(response)

        if "type" not in response:
            raise Exception("Invalid SKU found: " + toy_name)

        toy_type = CATEGORIES[response["type"]]
        toy_price = toy["price"]
        stock_type = "flop" if toy["is_flop"] else "ready made"

        return {
            "toy_name": toy_name,
            "toy_type": toy_type,
            "toy_price": toy_price,
            "stock_type": stock_type
        }

    def get_category(self):
        selected_category = None
        for category in CATEGORIES.keys():
            if self.args[category.lower()]:
                if selected_category is not None:
                    raise ValueError("Cannot have multiple categories selected")
                selected_category = category.lower()

        return selected_category

    def send_configured_notification(self, toy_type, toy_name, toy_price, stock_type):
        args = {
            "toy_type": toy_type,
            "toy_name": toy_name,
            "toy_price": toy_price,
            "stock_type": stock_type
        }

        title = self.config["notify-title"].format(**args)
        body = self.config["notify-text"].format(**args)
        notification.notify(title, body)

    def play_configured_sound(self):
        if self.config["audio-path"] is not None:
            playsound(self.config["audio-path"])

    def push_sns_notification(self, toy_type, toy_name, toy_price, stock_type):
        args = {
            "toy_type": toy_type,
            "toy_name": toy_name,
            "toy_price": toy_price,
            "stock_type": stock_type
        }
        
        # Construct title & body of SNS notification
        title = self.config["notify-title"].format(**args)
        body = self.config["notify-text"].format(**args)

        # Assume IAM role
        sts_client = boto3.client('sts')
        assumed_role_object = sts_client.assume_role(
            RoleArn=self.config["sns-role"],
            RoleSessionName="NotificationSession"
        )

        credentials = assumed_role_object["Credentials"]

        sns = boto3.client(
            'sns',
            aws_access_key_id = credentials["AccessKeyId"],
            aws_secret_access_key = credentials["SecretAccessKey"],
            aws_session_token = credentials["SessionToken"]
        )

        try:
            sns.publish(
                TopicArn = self.config["sns-topic"],
                Subject = title,
                Message = body
            )
        except ClientError as e:
            print(f"Could not publish message to topic: {str(e)}")
            raise e
        
        print(f"Message published to SNS topic. Title: {title} --- Body: {body}")

        # Exit so we don't spam the user with SMS notifications.
        sys.exit()


if __name__ == '__main__':
    main()
