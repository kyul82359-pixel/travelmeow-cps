rules_version = '2';
service cloud.firestore {
  match /databases/{database}/documents {

    // 프로필: 로그인한 사람 누구나 읽기(리더보드), 본인 것만 쓰기
    match /users/{uid} {
      allow read: if request.auth != null;
      allow create: if request.auth != null && request.auth.uid == uid
                    && request.resource.data.nickname is string
                    && request.resource.data.nickname.size() >= 2
                    && request.resource.data.nickname.size() <= 12
                    && request.resource.data.points is int
                    && request.resource.data.points <= 10;
      allow update: if request.auth != null && request.auth.uid == uid
                    // 포인트는 한 번에 최대 +10까지만 (조작 방지)
                    && request.resource.data.points is int
                    && request.resource.data.points >= resource.data.points
                    && request.resource.data.points <= resource.data.points + 10;
      allow delete: if false;
    }

    // 미션 제출: 본인 uid로만 생성, 수정·삭제 불가
    match /subs/{id} {
      allow read: if request.auth != null;
      allow create: if request.auth != null
                    && request.resource.data.uid == request.auth.uid
                    && request.resource.data.url is string
                    && request.resource.data.url.matches('https?://.*blog[.]naver[.]com/.*');
      allow update, delete: if false;
    }
  }
}
