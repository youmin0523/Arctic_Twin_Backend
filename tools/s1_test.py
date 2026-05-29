import requests

# 1. 방금 발급받은 키 입력 (여기를 수정하세요!)
CLIENT_ID = "sh-acc79726-2842-4cef-aa1e-f54f6e044f3f"
CLIENT_SECRET = "7qRjyWS4Vap1ZKPSZD7bFq5hD8oAEOod"

# 2. 인증 토큰 받아오기 (CDSE OAuth 2.0)
print("🔑 인증 토큰을 요청하는 중...")
auth_url = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
auth_data = {
    "grant_type": "client_credentials",
    "client_id": CLIENT_ID,
    "client_secret": CLIENT_SECRET,
}

response = requests.post(auth_url, data=auth_data)

# 에러가 발생하면 여기서 멈추고 이유를 출력합니다.
if response.status_code != 200:
    print("❌ 토큰 발급 실패! ID와 Secret을 다시 확인해주세요.")
    print("에러 내용:", response.text)
    exit()

access_token = response.json()["access_token"]
print("✅ 토큰 발급 성공!\n")

# 3. Sentinel-1 데이터 검색해보기 (CDSE OData API)
print("🛰️ 최근 Sentinel-1 위성 영상 5개 검색 중...")
search_url = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products"

# 쿼리 설명: 콜렉션 이름이 'SENTINEL-1'인 것 중, 최신순으로 정렬해서 5개만 가져오기
query = (
    "?$filter=Collection/Name eq 'SENTINEL-1'&$orderby=ContentDate/Start desc&$top=5"
)

headers = {"Authorization": f"Bearer {access_token}"}

search_response = requests.get(search_url + query, headers=headers)
search_response.raise_for_status()

results = search_response.json().get("value", [])

if not results:
    print("검색된 데이터가 없습니다.")
else:
    for i, item in enumerate(results, 1):
        print(f"[{i}] 파일명: {item['Name']}")
        print(f"    촬영일자: {item['ContentDate']['Start']}")
        print(f"    데이터크기: {round(item['ContentLength'] / (1024*1024), 2)} MB")
        print("-" * 50)
