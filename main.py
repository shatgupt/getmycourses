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

CURRENT_TERM = os.environ.get("CURRENT_TERM", "2201")  # "2201"  # Spring 2020
TEMP_DIR = os.environ.get("TEMP", "/tmp")
CLASSLIST_DIR = "classlist-responses"
LOCAL_CLASSLIST_DIR = os.path.join(TEMP_DIR, CLASSLIST_DIR, CURRENT_TERM)
CLOUD_CLASSLIST_DIR = f"{CLASSLIST_DIR}/{CURRENT_TERM}"
ASU_BASE_URL = "https://webapp4.asu.edu"
COURSE_FILE = "courses.json"
TEMP_FILE_SUFFIX = ".temp"
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
    "days": "Days",
    "time": "Time",
    "instructor": "Instructor",
    "non_reserved_open_seats": "Non Reserved Open Seats",
    "open_seats": "Open Seats",
    "title": "Title",
    "total_seats": "Total Seats",
}

# These global variables are persisted across Google Cloud function executions (non-cold-start ones)
app = Flask(__name__)
prev_classlist = {}
bucket = None
if os.environ.get("CLOUD_STORAGE_BUCKET"):
    bucket = storage.Client().get_bucket(os.environ.get("CLOUD_STORAGE_BUCKET"))

# Initialize things for making requests to ASU webapp
cj = CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

# Recreate the classlist dir to remove old local files
if os.path.isdir(LOCAL_CLASSLIST_DIR):
    shutil.rmtree(LOCAL_CLASSLIST_DIR)
os.makedirs(LOCAL_CLASSLIST_DIR, exist_ok=True)


def email_to_group(class_num, info):
    password = os.environ.get("EMAIL_LOGIN_PASSWORD")
    if not password:
        logging.warning(f"Not sending email as no login password set")
        return
    if not info["days"]:
        logging.warning(f"Not sending email as 'Days' not set for class: {class_num}")
        return

    msg = email.message.Message()
    msg[
        "Subject"
    ] = f"[{class_num}] {info['course']}: {info['title']} - {info['instructor']} [{info['days']} / {info['time']}]"
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

    # We are not putting it within try-catch to let it raise an exception
    # This exception will let us sleep if we hit max email sending limits
    server = smtplib.SMTP_SSL("smtp.gmail.com", 465)
    server.ehlo()
    server.login(msg["From"], password)
    server.sendmail(msg["From"], [msg["To"]], msg.as_string())
    server.close()


def load_previous_data(department):
    local_path = os.path.join(LOCAL_CLASSLIST_DIR, department, COURSE_FILE)
    local_temp_path = local_path + TEMP_FILE_SUFFIX

    # try to download from Cloud Storage if not present locally
    if not (os.path.isfile(local_path) or os.path.isfile(local_temp_path)):
        logging.info(f"{COURSE_FILE} NOT present locally.")
        if not bucket:
            logging.warning(
                "No Cloud Storage bucket configured. Not downloading any previous data."
            )
            return
        logging.info(f"Downloading previous data from Cloud Storage: {COURSE_FILE}")
        os.makedirs(os.path.join(LOCAL_CLASSLIST_DIR, department), exist_ok=True)
        cloud_path = f"{CLOUD_CLASSLIST_DIR}/{department}/{COURSE_FILE}"
        cloud_temp_path = cloud_path + TEMP_FILE_SUFFIX
        try:
            blob = bucket.blob(cloud_path)
            if blob.exists():
                blob.download_to_filename(local_path)
            else:
                blob = bucket.blob(cloud_temp_path)
                if blob.exists():
                    blob.download_to_filename(local_temp_path)
        except Exception as e:
            logging.warning(f"Downloading from Cloud Storage failed with error: {e}")

    # Give preference to complete file if present
    if os.path.isfile(local_path):
        path = local_path
    elif os.path.isfile(local_temp_path):
        path = local_temp_path
    else:
        logging.warning("No previous data present.")
        return

    try:
        with open(path) as f:
            prev_classlist[department] = json.load(f)
            logging.info(
                f"Loaded {len(prev_classlist[department])} classes from previous data"
            )
    except Exception as e:
        logging.warning(f"Couldn't load json {COURSE_FILE} with error: {e}")


def save_json(department, classlist, temp=False):
    # write classlist to dept.json in CLASSLIST_DIR
    os.makedirs(os.path.join(LOCAL_CLASSLIST_DIR, department), exist_ok=True)
    local_path = os.path.join(LOCAL_CLASSLIST_DIR, department, COURSE_FILE)
    if temp:
        local_path += TEMP_FILE_SUFFIX
    with open(local_path, "w") as f:
        json.dump(classlist, f)
    prev_classlist[department] = classlist

    # upload dept.json to Cloud Storage
    if bucket:
        try:
            cloud_path = f"{CLOUD_CLASSLIST_DIR}/{department}/{COURSE_FILE}"
            if temp:
                cloud_path += TEMP_FILE_SUFFIX
            blob = bucket.blob(cloud_path)
            blob.upload_from_filename(local_path)
        except Exception as e:
            logging.warning(f"Uploading to Cloud Storage failed with error: {e}")


def get_html(url):
    # app.logger.debug(url)
    req = urllib.request.Request(url, None, HEADERS)
    response = opener.open(req)
    content = response.read()
    return content.decode()


def get_clean_text(html):
    """
    Removes extra blank spaces and nbsp from html text.
    """
    return " ".join(html.text_content().split())


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
        class_num = get_clean_text(columns[2])
        classlist[class_num] = {
            "class_num": class_num,
            "course": get_clean_text(columns[0]),
            "title": get_clean_text(columns[1]),
            "instructor": get_clean_text(columns[3]),
            "dates": get_clean_text(columns[8]),
            "days": get_clean_text(columns[4]),
            "time": f"{get_clean_text(columns[5])} - {get_clean_text(columns[6])}",
        }
        seats = extract_classlist_seats(columns[10])
        classlist[class_num] = {**classlist[class_num], **seats}

    return classlist


# get all classses for a department, taking care of pagination
def get_all_classes(department, level):
    page = 1
    filters = f"t={CURRENT_TERM}&hon=F&promod=F&e=all&s={department}&page=%d"
    # filter by course level
    if level is not None:
        filters += f"&l={level}"
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
    level = request.args.get("level")
    if not department:
        return abort(400)
    if not prev_classlist.get(department):
        prev_classlist[department] = {}
        load_previous_data(department)
    else:
        # This works because prev_classlist is a global variable that is persisted
        # across Google Cloud function executions (non-cold-start ones)
        logging.info(f"{len(prev_classlist[department])} classes already loaded.")

    classlist = get_all_classes(department, level)

    updated_classlist = {}
    prev_dept_classlist = prev_classlist[department]
    # check if there is any updated class
    for class_num, class_info in classlist.items():
        prev_class_info = prev_dept_classlist.get(class_num, {})
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

    emailed_classlist = {}
    if updated_classlist:
        logging.info(f"Number of updated classes: {len(updated_classlist)}")
        logging.info(f"Updated classes: {updated_classlist}")
        # logging.error(f"Updated classes: {updated_classlist}")
        # post each class update to Google group
        for idx, (class_num, class_info) in enumerate(updated_classlist.items()):
            try:
                email_to_group(class_num, class_info)
            except Exception as e:
                logging.warning(f"Email sending error at update {idx+1}: {e}")
                # Make sure to save temporary progress till now so that we can
                # resume in next Cloud Function execution
                save_json(
                    department, {**prev_dept_classlist, **emailed_classlist}, True
                )
                raise Exception(
                    f"Email sending error at update {idx+1}"
                ).with_traceback(e.__traceback__)
            emailed_classlist[class_num] = class_info

        # Save final/complete classlist
        save_json(department, classlist)
    else:
        logging.info("No updated class.")

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
    app.run(debug=True)
