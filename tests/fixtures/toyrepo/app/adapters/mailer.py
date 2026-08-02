import requests

def send(row):
    return requests.post("https://api.sendgrid.com/v3/send", json=row)
