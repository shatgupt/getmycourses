# GetMyCourses

> Track your favorite course at ASU and get notified if a seat becomes available in it to register.

### NOTE: Tracking only CSE grad courses for now.

## How do I use this?

1. Just go to [this Google group](https://groups.google.com/forum/#!forum/asu-cse-class-seats) **using a desktop browser**.
2. Find and click the subject you are longing to slog for.
3. Click on options (down arrow) just below the title and select **Email updates to me**.

![Subscribe](https://lh3.googleusercontent.com/1wLeB2iEEELfoCC4cuU_mO1id2eAT9ANjzH05PO9VwUSa7cWaOjPifWl4RYPt3JKOiPFe-SgpePslOwu81N3L4tzEP_LryUnOb0d5Y5vmdYTcEOhAbxbJGEtJ9fI3EbQL7YxYirFoQ=w2400)

Thats it!

## No no, how do I use this *code*?

Ohh.

Python 3.6+ required. Clone this repo and follow these steps:
```sh
python3 -m venv /path/to/new/virtual/environment
pip install -r requirements.txt
python main.py
# You should have a Flask server running at http://127.0.0.1:5000
```

Get seats info of a particular class:
```
curl http://127.0.0.1:5000/class?class=30298
```

Get seats info of all classes of a department:
```
curl http://127.0.0.1:5000/classlist?department=CSE
```

If you want to post updates to a Google Group:

Create a Google Group which allows posting by email and make sure to export following environment variables:

- `TO_GROUP_EMAIL` - Email id of the Google Group where update is to be posted.
- `FROM_GROUP_EMAIL` - Gmail id used to send mail to Google Group. This user should have `posting` permissions to the group.
- `EMAIL_LOGIN_PASSWORD` - The Gmail login password for the above `FROM_GROUP_EMAIL` user. Make sure to enable 2FA and use an App Password here.
