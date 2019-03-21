import email.message
import json
import logging
import os
import re
import shutil
import smtplib
import urllib.request
from http.cookiejar import CookieJar

import lxml.html
from flask import Flask, abort, jsonify
from google.cloud import storage
from lxml.cssselect import CSSSelector

CLASSLIST_DIR = "classlist-responses/"
FULL_CLASSLIST_DIR = f"/tmp/{CLASSLIST_DIR}"
CURRENT_TERM = os.environ.get("CURRENT_TERM")  # "2197"  # Fall 2019
ASU_BASE_URL = "https://webapp4.asu.edu"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit"
        "/537.36 (KHTML, like Gecko) Chrome/70.0.3538.77 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Charset": "utf-8;q=0.7,*;q=0.3",
    "Accept-Language": "en-US,en;q=0.8",
    "Connection": "keep-alive",
}

INFO_STRINGS = {
    "class_num": "Class #",
    "course": "Course",
    "dates": "Dates",
    "instructor": "Instructor",
    "non_reserved_open_seats": "Non Reserved Open Seats",
    "open_seats": "Open Seats",
    "title": "Title",
    "total_seats": "Total Seats",
}

# Initialize things for making requests to ASU webapp
cj = CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

app = Flask(__name__)
prev_classlist = {}
bucket = None
if os.environ.get("CLOUD_STORAGE_BUCKET"):
    bucket = storage.Client().get_bucket(os.environ.get("CLOUD_STORAGE_BUCKET"))

# Recreate the classlist dir to remove old local files
if os.path.isdir(FULL_CLASSLIST_DIR):
    shutil.rmtree(FULL_CLASSLIST_DIR)
os.mkdir(FULL_CLASSLIST_DIR)


def email_to_group(class_num, info):
    password = os.environ.get("EMAIL_LOGIN_PASSWORD")
    if not password:
        logging.warn(f"Not sending email as no login password set")
        return

    msg = email.message.Message()
    msg[
        "Subject"
    ] = f"[{class_num}] {info['course']}: {info['title']} - {info['instructor']}"
    msg["From"] = os.environ.get("FROM_GROUP_EMAIL")
    msg["To"] = os.environ.get("TO_GROUP_EMAIL")
    msg.add_header("Content-Type", 'text/html; charset="UTF-8"')
    email_content = [
        "<h2>Class updated:</h2>",
        '<table style="text-align: center; border: 1px solid black;">',
        "<tr>",
        "\n".join(
            (
                '<th style="font-weight: bold; border-right: 1px solid black; border-'
                f'bottom: 1px solid black; padding: 5px;">{INFO_STRINGS[k]}</th>'
            )
            for k in info
        ),
        "</tr>",
        "<tr>",
        "\n".join(
            (
                '<td style="border-right: 1px solid black; padding: 5px;">'
                f'<a href="{ASU_BASE_URL}/catalog/course?r={class_num}">{v}</a></td>'
            )
            for k, v in info.items()
        ),
        "</tr>",
        "</table>",
    ]
    msg.set_payload("\n".join(email_content))

    try:
        server = smtplib.SMTP_SSL("smtp.gmail.com", 465)
        server.ehlo()
        server.login(msg["From"], password)
        server.sendmail(msg["From"], [msg["To"]], msg.as_string())
        server.close()
    except Exception as e:
        logging.error(f"Send email error: {e}")


def load_previous_data(department):
    fname = f"{department}.json"
    local_path = f"{FULL_CLASSLIST_DIR}{fname}"

    # try to download from Cloud Storage if not present locally
    if not os.path.isfile(local_path):
        logging.info(f"{fname} NOT present locally.")
        if bucket:
            logging.info(f"Downloading previous data from Storage: {fname}")
            try:
                blob = bucket.blob(f"{CLASSLIST_DIR}{fname}")
                blob.download_to_filename(local_path)
            except Exception as e:
                logging.warn(f"Downloading from Cloud Storage failed with error: {e}")
                return

    try:
        with open(local_path) as f:
            prev_classlist[department] = json.load(f)
    except Exception as e:
        logging.warn(f"Couldn't load json {fname} with error: {e}")


def get_html(url):
    # app.logger.debug(url)
    req = urllib.request.Request(url, None, HEADERS)
    response = opener.open(req)
    content = response.read()
    return content.decode()


def extract_class_seats(html):
    matches = re.findall(
        (
            r"<!-- Open seats -->[\s\S]*Open: <\/label>\D*(\d*)&nbsp;of&nbsp;(\d*)"
            r"\D*<span[\s\S]*<!-- End of open seat -->"
        ),
        html,
    )
    if not matches:
        raise RuntimeError("No regex match for open seats!")
    seats = {"open_seats": matches[0][0], "total_seats": matches[0][1]}
    matches = re.findall(r"Non Reserved Available Seats:\D*(\d*)", html)
    if not matches:
        return seats
    seats["non_reserved_open_seats"] = matches[0]
    return seats


def extract_classlist_seats(column):
    text = column.text_content().strip().split()
    seats = {"total_seats": text[2], "open_seats": text[0]}
    # extract reserved seats info
    get_reserve = CSSSelector("span.rsrvtip")
    reserve = get_reserve(column)
    if not reserve:  # meaning that the class had no reservation
        return seats
    reserve = reserve[0]
    r_html = get_html(f"{ASU_BASE_URL}{reserve.get('rel')}")
    matches = re.findall(r"Non Reserved Available Seats :\D*(\d*)", r_html)
    if not matches:
        raise RuntimeError("No regex match for Non Reserved Available Seats!")
    seats["non_reserved_open_seats"] = matches[0]
    return seats


def extract_classlist_info(tree):
    get_table = CSSSelector("table#CatalogList")
    table = get_table(tree)
    if not table:
        return "No table found!"
    table = table[0]
    get_rows = CSSSelector(".grpEven,.grpOdd,.grpEvenTitle,.grpOddTitle")
    rows = get_rows(table)
    if not rows:
        return "No rows in table!"

    # Extract info from rows
    # split and join to remove the \t and \n between texts
    get_columns = CSSSelector("td")
    classlist = {}
    for row in rows:
        columns = get_columns(row)
        class_num = " ".join(columns[2].text_content().split())
        classlist[class_num] = {
            "class_num": class_num,
            "course": " ".join(columns[0].text_content().split()),
            "title": " ".join(columns[1].text_content().split()),
            "instructor": " ".join(columns[3].text_content().split()),
            "dates": " ".join(columns[8].text_content().split()),
        }
        seats = extract_classlist_seats(columns[10])
        classlist[class_num] = {**classlist[class_num], **seats}

    return classlist


# get all classses for a department, taking care of pagination
def get_all_classes(department):
    page = 1
    filters = f"t={CURRENT_TERM}&hon=F&promod=F&e=all&s={department}&page=%d"
    url = f"{ASU_BASE_URL}/catalog/myclasslistresults?{filters}"
    html = get_html(url % page)
    tree = lxml.html.fromstring(html)
    classlist = extract_classlist_info(tree)

    # check if more pages in result
    pages = 1
    get_pages = CSSSelector(".pagination>li")
    page_list = get_pages(tree)
    # if only 1 page, then only 1 li
    # if more than 1 page, there will also be next button
    if len(page_list) > 2:
        pages = len(page_list) - 1

    for page in range(2, pages + 1):
        html = get_html(url % page)
        tree = lxml.html.fromstring(html)
        classlist = {**classlist, **extract_classlist_info(tree)}

    return classlist


# https://webapp4.asu.edu/catalog/coursedetails?r=30298
def handle_get_class(request):
    class_num = request.args.get("class")
    if not class_num:
        return abort(400)

    filters = f"t={CURRENT_TERM}&r={class_num}"
    html = get_html(f"{ASU_BASE_URL}/catalog/coursedetails?{filters}")
    seats = extract_class_seats(html)
    return jsonify(seats)


# https://webapp4.asu.edu/catalog/classlist?e=all&l=grad&s=CSE
def handle_get_classlist(request):
    department = request.args.get("department")
    if not department:
        return abort(400)
    if not prev_classlist.get(department):
        prev_classlist[department] = {}
        load_previous_data(department)

    classlist = get_all_classes(department)

    updated_classlist = {}
    # check if there is any updated class
    for class_num, class_info in classlist.items():
        prev_class_info = prev_classlist[department].get(class_num, {})
        prev_os = int(prev_class_info.get("open_seats", -1))
        cur_os = int(class_info.get("open_seats", -1))
        prev_nros = int(prev_class_info.get("non_reserved_open_seats", -1))
        cur_nros = int(class_info.get("non_reserved_open_seats", -1))
        # We prefer tracking updates only for non_reserved_open_seats, if present,
        # otherwise open_seats
        if cur_nros >= 0:  # this should mean that reservation info was present
            if cur_nros != prev_nros:
                updated_classlist[class_num] = class_info
        elif cur_os != prev_os:  # implicit that there was no reservation
            updated_classlist[class_num] = class_info

    if updated_classlist:
        logging.info(f"Num Updated classes: {len(updated_classlist)}")
        logging.info(f"Updated classes: {updated_classlist}")
        # app.logger.error(f"Updated classes: {updated_classlist}")
        # post each class update to Google group
        for class_num, class_info in updated_classlist.items():
            email_to_group(class_num, class_info)

        # write classlist to dept.json in CLASSLIST_DIR
        fname = f"{department}.json"
        local_path = f"{FULL_CLASSLIST_DIR}{fname}"
        with open(local_path, "w") as f:
            json.dump(classlist, f)
        prev_classlist[department] = classlist

        # upload dept.json to Cloud Storage
        if bucket:
            try:
                blob = bucket.blob(f"{CLASSLIST_DIR}{fname}")
                blob.upload_from_filename(local_path)
            except Exception as e:
                logging.warn(f"Uploading to Cloud Storage failed with error: {e}")

    return jsonify(classlist)


# This function handles request from local Flask server
@app.route("/class", methods=["GET"])
def flask_get_class():
    from flask import request

    return handle_get_class(request)


# This function handles request from local Flask server
@app.route("/classlist", methods=["GET"])
def flask_get_classlist():
    from flask import request

    return handle_get_classlist(request)


# This function handles Google Cloud Functions HTTP request
def get_classlist(request):
    if request.method != "GET":
        return abort(405)
    return handle_get_classlist(request)


# This function handles Google Cloud Functions HTTP request
def get_class(request):
    if request.method != "GET":
        return abort(405)
    return handle_get_class(request)


if __name__ == "__main__":
    app.run()
