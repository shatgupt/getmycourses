import json
import logging
import os
import re
import urllib.request
from http.cookiejar import CookieJar

import lxml.html
from flask import Flask, abort, jsonify
from lxml.cssselect import CSSSelector

CLASSLIST_DIR = "/tmp/classlist/"
CURRENT_TERM = "2191"  # Spring 2019
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

# Initialize things for making requests to ASU webapp
cj = CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

app = Flask(__name__)
prev_classlist = {}

if not os.path.isdir(CLASSLIST_DIR):
    os.mkdir(CLASSLIST_DIR)


def load_previous_data(department):
    fname = f"{CLASSLIST_DIR}{department}.json"

    if not os.path.isfile(fname):
        # try to download from Cloud Storage
        logging.info(f"Downloading previous data from Storage: {department}.json")

    try:
        with open(fname) as f:
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
    seats = {"open_seats": text[0], "total_seats": text[2]}
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


def extract_classlist_info(html):
    tree = lxml.html.fromstring(html)
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
            "course": " ".join(columns[0].text_content().split()),
            "title": " ".join(columns[1].text_content().split()),
            "instructor": " ".join(columns[3].text_content().split()),
            "dates": " ".join(columns[8].text_content().split()),
        }
        seats = extract_classlist_seats(columns[10])
        classlist[class_num] = {**classlist[class_num], **seats}

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

    filters = f"t={CURRENT_TERM}&l=grad&hon=F&promod=F&e=all&s={department}&page=1"
    html = get_html(f"{ASU_BASE_URL}/catalog/myclasslistresults?{filters}")
    classlist = extract_classlist_info(html)

    updated_classlist = {}
    # check if there is any updated class
    for class_num, class_info in classlist.items():
        if class_info != prev_classlist[department].get(class_num):
            updated_classlist[class_num] = class_info

    if updated_classlist:
        # post each class update to Google group

        # write classlist to dept.json in CLASSLIST_DIR
        fname = f"{CLASSLIST_DIR}{department}.json"
        with open(fname, "w") as f:
            json.dump(classlist, f)
        prev_classlist[department] = classlist

        # upload dept.json to Cloud Storage

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
