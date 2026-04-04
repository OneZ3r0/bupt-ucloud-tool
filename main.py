import requests
import re
import getpass

# 第一步
# GET BASIC_AUTH_URL，从响应中获取 cookie 和 <input name="execution" value="xxx"> 的 execution 参数

BASIC_AUTH_URL = (
    "https://auth.bupt.edu.cn/authserver/login?service=https://ucloud.bupt.edu.cn"
)

res = requests.get(BASIC_AUTH_URL)
# print(res.status_code)
# print(res.text)

EXECUTION_VALUE_RE = re.compile(
    r'<input name="execution" value="(.*?)"'
)  # ?非贪婪，到"停止
if m := EXECUTION_VALUE_RE.search(res.text):
    execution_value = m.group(1)
    # print("execution:", execution_value)

# print(type(res.cookies))


# 第二步
# POST 请求 BASIC_AUTH_URL，携带 cookie 和 execution 参数，以及用户名和密码
# 传参 username password 需要用户输入
username = input("请输入用户名：")
password = getpass.getpass("请输入密码：")

basic_auth_data = {
    "username": username,
    "password": password,
    "submit": "LOGIN",
    "type": "username_password",
    "execution": execution_value,
    "_eventId": "submit",
}

# add headers Referer: https://auth.bupt.edu.cn/authserver/login?service=https://ucloud.bupt.edu.cn
# headers = {
#     "Referer": "https://auth.bupt.edu.cn/authserver/login?service=https://ucloud.bupt.edu.cn"
# }

res2 = requests.post(
    BASIC_AUTH_URL, data=basic_auth_data, cookies=res.cookies, allow_redirects=False
)

# 第三步
# 从第二步 响应中得到 Location 的 ticket
location = res2.headers["Location"]
ticket = location.split("ticket=")[-1]
# print("ticket:", ticket)
# parsed_url = urlparse(location)
# ticket = parse_qs(parsed_url.query).get("ticket", [None])[0]


# 第四步

# 和 ykt 有关的都要附带
# Authorization: Basic  cG9ydGFsOnBvcnRhbF9zZWNyZXQ=
# Tenant-Id: 000000
# Identity: JS005:undefined 这个后续使用

headers = {
    "Authorization": "Basic  cG9ydGFsOnBvcnRhbF9zZWNyZXQ=",
    "Tenant-Id": "000000",
    "Identity": "JS005:undefined",
}

# POST 请求 API_AUTH_URL，携带 ticket 参数，获取 jwt，解码可以得到 user_id
API_AUTH_URL = "https://apiucloud.bupt.edu.cn/ykt-basics/oauth/token"

api_auth_data = {
    "ticket": ticket,
    "grant_type": "third",
}

res3 = requests.post(API_AUTH_URL, data=api_auth_data, headers=headers)
# print(res3.status_code)
# print(res3.text)

info = res3.json()
# print(info)
jwt_token = info["access_token"]
# print(jwt_token)

user_id = info["user_id"]

# ---------------------------
# 前面是完成基本信息收集，下面是获取各种信息

BASE_URL = "https://apiucloud.bupt.edu.cn"

# 获取课程信息，需要 headers
courses = "/ykt-site/site/list/student/current"
params = {
    "size": 999999,
    "current": 1,
    "userId": user_id,
    "siteRoleCode": 2,
}

headers["Blade-Auth"] = jwt_token

res4 = requests.get(BASE_URL + courses, params=params, headers=headers)
# print(res4.status_code)
# print(res4.json())

courses_list = res4.json()["data"]["records"]
print(len(courses_list))
for course in courses_list:
    print(course["siteName"], course["id"])


# 从 json 中提取数据信息，获得课程 id

course_resources = "/ykt-site/site-resource/tree/student"

params2 = {
    "userId": user_id,
    "siteId": courses_list[7]["id"],
}
# 根据课程 id 获取课程的资源信息，如 pdf 课件等，提供一个简单的列表，并支持下载

res5 = requests.post(BASE_URL + course_resources, params=params2, headers=headers)
# print(res5.status_code)
# print(res5.text)

resources = res5.json()["data"]
for resource in resources:
    # print(resource)
    print(resource["resourceName"])
    if attachments := resource["attachmentVOs"]:
        for attachment in attachments:
            resource = attachment["resource"]
            print(
                resource["name"],
                resource["url"],
                resource["fileSizeUnit"],
            )
    # print(resource["name"], resource["url"])
