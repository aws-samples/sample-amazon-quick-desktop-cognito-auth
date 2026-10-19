# Cognito와 Amazon Quick Desktop 설정 가이드

언어: [English](SETUP.md) · **한국어**

[저장소 개요](../README.md) · [설정 및 운영 참고 문서 — 영문](CONFIGURATION.md) · [문제 해결 — 영문](../TROUBLESHOOTING.md)

이 가이드는 인증 스택 배포부터 Amazon Quick 연결, 사용자의 첫 Desktop 로그인 확인까지
안내합니다. 관리자 설정은 한 번 완료하면 됩니다. 이후 사용자를 추가할 때는 사용자 등록과
로그인 단계를 반복합니다.

예제는 **Bash**, AWS 리전 `us-east-1`을 사용합니다. Identity Center 자동 프로비저닝을
선택하는 경우 그룹 이름은 `QuickUsers`를 사용합니다. 리전과 그룹은 실제 환경에 맞게
변경하세요. 기존 Quick 계정이나 디렉터리를 이 예제 설정으로 교체할 필요는 없습니다.

스크린샷은 작성자의 [원본 한국어 설정 가이드](https://madebybk.notion.site/amazon-quick-desktop-enterprise-idp-cognito-idc)에서
가져왔습니다. 화면에서 어떤 항목을 선택하는지 참고하되, 엔드포인트와 클라이언트 값은
반드시 **본인이 배포한 스택의 출력값**을 사용하세요. 일부 스크린샷의 **Create extension**은
현재 문서에서 **Add extension**으로 안내하는 버튼입니다. 계정 정보는 가리거나 잘라냈습니다.
[스크린샷 출처](images/README.md)를 참고하세요.

콘솔에서 항목을 쉽게 찾을 수 있도록 버튼, 메뉴, 필드 이름은 스크린샷과 같은 영문 표기를
유지했습니다.

## 진행 순서

1. [Quick 계정과 권한 확인](#1-quick-계정과-권한-확인)
2. [사용자 관리 방식 선택 및 터미널 준비](#2-사용자-관리-방식-선택-및-터미널-준비)
3. [CDK 스택 두 개 배포](#3-cdk-스택-두-개-배포)
4. [Quick에서 Extension access 추가](#4-quick에서-extension-access-추가)
5. [Desktop Extension 생성 및 활성화](#5-desktop-extension-생성-및-활성화)
6. [Quick과 Cognito에 사용자 등록](#6-quick과-cognito에-사용자-등록)
7. [Desktop 설치 및 첫 로그인](#7-desktop-설치-및-첫-로그인)
8. [연동 검증 및 사용자 안내](#8-연동-검증-및-사용자-안내)
9. [테스트 배포 정리](#9-테스트-배포-정리)

## 1. Quick 계정과 권한 확인

배포 전에 다음 항목을 준비하세요.

| 항목 | 확인 사항 |
| --- | --- |
| Quick 구독 | 활성화된 **Amazon Quick Enterprise** 계정이 있어야 합니다. Free/Plus 계정 로그인은 별도 방식입니다. |
| Quick 홈 리전 | Desktop을 포함한 Quick 전체 기능을 지원해야 합니다. [AWS 리전별 지원 현황](https://docs.aws.amazon.com/quick/latest/userguide/regions.html#regions-qs)을 확인하세요. Quick Sight 기능만 지원하는 리전으로는 충분하지 않습니다. |
| Quick 관리자 | **Manage account**에 접근하고, **Permissions → Extension access**를 설정하며, Extension을 생성할 수 있어야 합니다. |
| 첫 테스트 사용자 | 해당 Quick 계정의 웹 애플리케이션에 로그인할 수 있는 활성 사용자입니다. 대소문자를 포함한 정확한 이메일 주소를 기록하세요. |
| AWS 배포 권한 | 대상 AWS 계정에서 CDK bootstrap을 수행하고 스택의 IAM 역할, Cognito, Lambda, API Gateway, CloudFront, WAF, Secrets Manager, 로그 리소스를 생성할 수 있는 자격 증명이 필요합니다. Identity Center 모드에는 템플릿의 디렉터리, 큐, 이벤트 및 선택적 CloudTrail 권한도 필요합니다. AWS 관리자에게 합성된 템플릿의 권한 검토를 요청하세요. |
| 사용자 기기 | 지원되는 macOS 또는 Windows 기기, 초대 메일을 받을 수 있는 메일함, Cognito TOTP MFA용 인증 앱이 필요합니다. [Desktop 시스템 요구 사항](https://docs.aws.amazon.com/quick/latest/userguide/getting-started-desktop.html#desktop-prerequisites)을 확인하세요. |
| 네트워크 | 기기와 브라우저에서 Quick, Cognito, 스택의 CloudFront 도메인 및 로컬 OAuth 콜백에 접근할 수 있어야 합니다. IT 팀과 함께 [공식 네트워크 허용 목록](https://docs.aws.amazon.com/quick/latest/userguide/desktop-security.html#desktop-network-access)을 확인하세요. |

Quick을 이미 사용 중이라면 기존 구독과 로그인 방식을 유지하세요. Desktop Enterprise
로그인은 Identity Center, IAM Federation, Quick 자체 사용자 계정을 지원합니다.
**스택의 `useIdc` 설정은 Cognito 사용자 프로비저닝 방식을 선택하는 옵션이며,
Quick 계정의 자격 증명 유형을 변경하지 않습니다.**

Quick Enterprise 계정이 없다면 먼저 관리자가 구독을 생성하고 최초 Quick 관리자와
사용자를 등록해야 합니다. AWS Management Console에서 **Amazon Quick**을 열고
조직의 자격 증명 및 구독 정책에 따라 계정 설정을 완료하세요.
이 CDK 스택은 Quick 구독을 생성하거나 Quick 사용자를 할당하지 않습니다.

새 Quick 계정에서 Identity Center를 사용할 경우, Quick 구독을 생성하기 **전에**
2.1단계의 인스턴스, 사용자, 관리자 그룹을 먼저 준비한 다음 가입 과정에서 해당
인스턴스를 선택하세요. Quick 계정을 생성한 후에는 인증 방식을 변경할 수 없습니다.
[Quick과 Identity Center 구성](https://docs.aws.amazon.com/quick/latest/userguide/setting-up-sso.html)을 참고하세요.

**Quick 관리자 사용자로 Quick 웹 애플리케이션에 로그인**하세요. AWS IAM 콘솔 세션으로만
접근하면 일부 관리 메뉴만 표시될 수 있습니다. 아래 배너가 보이면 **Quick Sign In**을
선택하여 Quick 로그인을 완료하세요.

![IAM 권한으로만 접근한 경우 표시되는 배너와 Quick Sign In 버튼](images/quick-web-sign-in.png)

**확인:** 관리자가 Quick 애플리케이션에 접근할 수 있고, 테스트 사용자도 대상 Quick 계정에
웹으로 로그인할 수 있어야 합니다.

## 2. 사용자 관리 방식 선택 및 터미널 준비

### 2.1 사용할 모드 선택

| 모드 | 적합한 경우 | 준비 사항 |
| --- | --- | --- |
| **A — Cognito 단독** | 가장 간단하게 구성하고 Cognito 사용자를 직접 생성하려는 경우 | 스택에서 Identity Center 디렉터리나 동기화 그룹을 요구하지 않습니다. |
| **B — Identity Center 자동 프로비저닝** | 기존 Identity Center 그룹을 기준으로 Cognito 사용자 목록을 관리하려는 경우 | 그룹을 준비하고 해당 인스턴스의 기본 리전에 배포합니다. |

**모드 B**를 사용할 경우 대상 AWS 계정과 리전에서 IAM Identity Center를 엽니다.

1. 조직에서 이미 관리하는 디렉터리 인스턴스를 사용하세요. 없다면 먼저 자격 증명 관리자에게
   적절한 인스턴스 활성화를 요청합니다.
   단일 AWS 계정에서 격리된 평가를 진행한다면, 생성이 허용된 환경에서 Quick이 지원하는
   [계정 인스턴스](https://docs.aws.amazon.com/singlesignon/latest/userguide/account-instances-identity-center.html)를
   사용할 수 있습니다. 이 샘플은 배포 자격 증명으로 조회할 수 있는 계정 인스턴스와
   조직 인스턴스를 모두 사용할 수 있습니다.
2. **Groups**에서 그룹을 생성하거나 선택합니다. 예제 그룹 이름은 `QuickUsers`입니다.
3. 테스트 사용자를 그룹에 추가합니다. 사용자의 Identity Center 이메일은 활성 Quick 사용자의
   이메일과 정확하게 일치해야 합니다.
4. 그룹의 표시 이름과 인스턴스의 기본 리전을 기록합니다. 접근 가능한 인스턴스가 여러 개라면
   사용할 인스턴스 ARN도 기록합니다.

![Identity Center에서 예제 QuickUsers 그룹 생성](images/identity-center-group.png)

스택은 지정한 그룹이 없으면 생성할 수 있지만, Identity Center 사용자를 생성하거나 그룹에
사용자를 추가하지는 않습니다. 배포 후 기존 그룹 구성원을 가져와 Cognito에 등록합니다.
동기화 그룹에 추가하는 것만으로 Quick 구독이나 역할이 부여되지는 않으므로,
해당 사용자의 Quick 접근 권한도 별도로 준비해야 합니다.

AWS 액세스 포털에서는 **Applications**를 열고 Quick 계정을 선택하세요.
계정 인스턴스는 AWS 계정용 권한 세트를 제공하지 않으므로 **Accounts** 탭이
비어 있어도 정상입니다.

### 2.2 Bash 터미널 열기

AWS 콘솔 하단 도구 모음의 **AWS CloudShell**을 사용할 수 있습니다. CloudShell은 현재
로그인한 AWS 세션의 자격 증명을 사용합니다. 로컬 Bash 터미널을 사용한다면 대상 계정의
AWS CLI 자격 증명이 미리 설정되어 있어야 합니다.

![CloudShell 터미널과 하단 도구 모음의 실행 버튼](images/cloudshell.png)

다음 명령을 실행합니다.

```bash
aws --version
git --version
node --version
npm --version
aws sts get-caller-identity --query '{Account:Account,Arn:Arn}' --output table
```

AWS CLI v2와 **Node.js 22 이상**을 사용하세요. 도구가 없거나 Node.js 버전이 낮으면
지원되는 버전을 설치한 후 진행합니다. 로컬 Python은 선택 사항인
[개발자 테스트](CONFIGURATION.md#local-validation)에만 필요하며, 배포에는 필요하지 않습니다.

마지막 명령에 표시되는 AWS 계정 ID가 올바른지 확인하세요. 배포가 끝날 때까지 같은
터미널을 사용해야 아래에서 설정할 변수와 CDK 옵션이 유지됩니다.

### 2.3 리전 설정 및 저장소 복제

모드 A의 예제 리전은 `us-east-1`입니다. 모드 B에서는 선택한 Identity Center 인스턴스의
**기본 리전**으로 변경하세요. 인증 스택을 다른 리전에 배포하더라도 WAF 스택은 항상
`us-east-1`을 사용합니다.

```bash
export AWS_REGION=us-east-1
export AWS_DEFAULT_REGION="$AWS_REGION"
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

git clone https://github.com/aws-samples/sample-amazon-quick-desktop-cognito-auth.git
cd sample-amazon-quick-desktop-cognito-auth
npm ci
```

`npm ci`는 저장소의 lockfile에 기록된 버전을 설치합니다.
CDK를 별도로 설치하거나 업그레이드할 필요는 없습니다.

### 2.4 선택한 모드의 CDK 옵션 설정

다음 두 블록 중 **하나만** 실행하세요.

**모드 A — Cognito 단독:**

```bash
CDK_OPTIONS=(-c useIdc=false)
```

**모드 B — Identity Center 자동 프로비저닝:**

```bash
CDK_OPTIONS=(-c useIdc=true -c idcGroupName=QuickUsers)
```

모드 B에서는 `QuickUsers`를 실제 그룹의 정확한 표시 이름으로 변경하세요. 이름에 공백이
있으면 `idcGroupName=...` 인수 전체를 따옴표로 감쌉니다. 인스턴스가 여러 개 보이면 앞서
기록한 ARN으로 `-c idcInstanceArn=YOUR_INSTANCE_ARN`을 이 배열에 추가합니다.

계정에 기존 API Gateway 로깅 역할이 있는지 확인합니다.

```bash
aws apigateway get-account --region "$AWS_REGION" \
  --query cloudwatchRoleArn --output text
```

기존 역할 ARN이 반환되면 AWS 관리자에게 API Gateway 로깅에 사용할 수 있는 역할인지
확인한 후, 다음 옵션을 추가하여 기존 역할을 유지합니다.

```bash
CDK_OPTIONS+=(-c apiGatewayAccountRole=false)
```

`None`이 반환되면 스택이 역할을 생성하도록 기본 설정을 유지합니다.
접근 거부가 발생하면 진행하기 전에 AWS 관리자에게 해당 리전의 설정을 확인해 달라고
요청하세요. 모드 B에서는 기존 관리 이벤트 추적을 `-c createTrail=false`로 재사용할지도
확인합니다. 자세한 내용은 [설정 참고 문서](CONFIGURATION.md#account-and-region-constraints)를
참고하세요.

기존 추적을 재사용하기 전에 해당 인스턴스의 기본 리전에서 필요한 Identity Center
관리 이벤트를 실제로 로깅하고 있는지 확인하세요.

**확인:** AWS 계정, 인증 리전, 배포 모드가 선택되어 있어야 합니다.
이후 모든 CDK 명령에 동일한 `"${CDK_OPTIONS[@]}"`를 포함합니다.
bootstrap 명령에도 필요합니다. 애플리케이션은 `useIdc=false`를 지정하지 않으면
기본적으로 Identity Center 그룹을 요구합니다.

## 3. CDK 스택 두 개 배포

### 3.1 필요한 리전에서 bootstrap 수행

```bash
npx cdk bootstrap "aws://${AWS_ACCOUNT_ID}/${AWS_REGION}" "${CDK_OPTIONS[@]}"

if [ "$AWS_REGION" != "us-east-1" ]; then
  npx cdk bootstrap "aws://${AWS_ACCOUNT_ID}/us-east-1" "${CDK_OPTIONS[@]}"
fi
```

CDK 배포에 필요한 리소스를 준비하는 단계입니다. 평소 사용하는 배포 역할에 해당 리소스를
생성할 권한이 없다면 관리자가 bootstrap을 수행해야 할 수 있습니다.

### 3.2 변경 사항 검토 및 배포

```bash
npx cdk synth "${CDK_OPTIONS[@]}"
npx cdk diff "${CDK_OPTIONS[@]}"

mkdir -p .local
npx cdk deploy --all "${CDK_OPTIONS[@]}" \
  --outputs-file .local/deployment-outputs.json
```

생성 또는 변경될 리소스와 IAM 권한을 검토한 후 CDK 승인 프롬프트에 응답하세요.
배포가 완료될 때까지 기다립니다. CloudFront 배포에는 몇 분 정도 걸릴 수 있습니다.

스택 선택 인수를 생략하면 `cdk synth`는 두 스택을 모두 합성하고, `cdk diff`는
두 스택을 모두 비교합니다. `--all` 옵션은 `cdk deploy`에 사용합니다.

`--all`은 다음 두 스택을 필요한 순서대로 배포합니다.

| 스택 | 리전 | 용도 |
| --- | --- | --- |
| `QuickDesktopCognitoIdcStack` | `$AWS_REGION` | Cognito, OAuth 프록시, CloudFront 및 선택한 경우 사용자 프로비저닝 |
| `QuickDesktopCloudFrontWafStack` | `us-east-1` | CloudFront용 WAF |

스택은 `us-east-1`의 WAF 스택을 포함하여 일부 리소스에 고정된 이름을 사용합니다.
처음 설정할 때는 이 샘플이 이미 설치되어 있지 않은 계정을 사용하세요.
기존 배포를 변경한다면 먼저 [업그레이드 및 리소스 보존 안내](CONFIGURATION.md#updating-an-existing-deployment)를
확인합니다.

### 3.3 출력값 확인 및 기록

배포 결과, `.local/deployment-outputs.json` 파일 또는 인증 리전의
**CloudFormation → QuickDesktopCognitoIdcStack → Outputs**에서 값을 확인합니다.
나중에 다음 명령으로 다시 조회할 수도 있습니다.

```bash
aws cloudformation describe-stacks \
  --region "$AWS_REGION" \
  --stack-name QuickDesktopCognitoIdcStack \
  --query 'Stacks[0].Outputs' \
  --output table
```

`PoolId`, `ClientId`, `IssuerUrl`, `AuthEndpoint`, `TokenEndpoint`, `JwksUri`를
기록합니다. `CognitoReadiness`가 `READY`인지도 확인하세요.

- `IssuerUrl`과 `JwksUri`는 Cognito를 직접 가리킵니다.
- `AuthEndpoint`와 `TokenEndpoint`는 이번 배포의 CloudFront 프록시를 가리킵니다.
- `ClientId`는 Cognito의 퍼블릭 앱 클라이언트 ID입니다. 복사할 클라이언트 시크릿은 없습니다.

이처럼 호스트가 서로 다른 것은 의도된 구성입니다. 스크린샷을 보고 URL을 직접 조합하거나
Cognito의 직접 인증/토큰 엔드포인트로 대체하지 말고, 출력값을 그대로 복사하세요.

**확인:** 두 스택 모두 `CREATE_COMPLETE` 또는 `UPDATE_COMPLETE` 상태이고,
`CognitoReadiness=READY`여야 합니다. 이는 인프라와 메타데이터 준비가 완료되었다는 의미입니다.
Quick 로그인에는 다음 설정도 필요합니다.

## 4. Quick에서 Extension access 추가

Cognito 클라이언트와 엔드포인트를 Quick에 등록하는 단계입니다.

1. Quick 웹 애플리케이션을 열고 대상 Quick 계정을 선택합니다.
2. 오른쪽 상단 계정/프로필 메뉴에서 **Manage account**를 선택합니다.

   <img src="images/manage-account.png" alt="Manage account가 강조된 Quick 계정 메뉴. 계정 이름과 사용자 이름은 가려져 있습니다." width="280">

3. **Permissions** 아래에서 **Extension access**를 선택합니다.

   <img src="images/extension-access-navigation.png" alt="Permissions 메뉴에서 Extension access 선택" width="340">

4. **Add extension access**를 선택합니다.
5. **Amazon Quick (Desktop application for Quick)**을 선택하고 **Next**를 누릅니다.

   ![Extension 서비스로 Amazon Quick 선택](images/select-desktop-service.png)

6. 다음 값을 입력합니다.

| Quick 필드 | 입력값 |
| --- | --- |
| Name | `QuickDesktop-access` 또는 공백 없이 영문자, 숫자, 하이픈으로 구성한 이름 |
| Description | 선택 사항. 예: `Cognito sign-in for Quick Desktop` |
| Issuer URL | `IssuerUrl` 출력값 그대로 |
| Authorization Endpoint | `AuthEndpoint` 출력값 그대로 |
| Token Endpoint | `TokenEndpoint` 출력값 그대로 |
| JWKS URI | `JwksUri` 출력값 그대로 |
| Client ID | `ClientId` 출력값 그대로 |

![Extension access 상세 입력 화면. 예시 값 대신 스택 출력값을 입력하세요.](images/extension-access-fields.png)

7. **클라이언트 및 엔드포인트 필드 다섯 개를 모두** 같은 스택의 출력값과 비교합니다.
   불필요한 공백, 누락된 경로, 끝에 추가된 슬래시가 없는지 확인하세요.
8. **Add**를 선택합니다. 지정한 이름의 항목이 목록에 표시되고 유형이 **Amazon Quick**인지
   확인합니다.

Extension access 값은 생성 후 수정할 수 없습니다. 잘못 입력했다면 올바른 값으로 다시
생성해야 합니다. 이미 여러 사용자가 사용하는 연동을 변경할 때는 해당 관리자와 일정을
조율하세요.

**확인:** **Permissions → Extension access**에 `QuickDesktop-access`가 표시되어야 합니다.
이 항목만으로는 Desktop 로그인이 활성화되지 않으므로 반드시 5단계까지 진행하세요.

## 5. Desktop Extension 생성 및 활성화

1. Quick 메인 애플리케이션으로 돌아갑니다.
2. 왼쪽 메뉴에서 **Extensions**를 선택합니다. 화면 구성에 따라 **More** 또는
   **Connect apps and data** 아래에 있을 수 있습니다.
3. **Add extension**을 선택합니다. 스크린샷에서는 **Create extension**으로 표시됩니다.

   ![Quick Extensions 화면에서 Create extension 선택](images/create-extension.png)

4. 4단계에서 만든 **Desktop application for Quick** 접근 설정을 선택합니다.
5. **Next**, **Create** 순서로 선택합니다.
6. Desktop Extension이 표시되고 상태가 **Active**인지 확인합니다.

![Active 상태의 Quick Desktop Extension 카드](images/extension-active.png)

이는 관리자가 계정 단위로 한 번 수행하는 작업입니다. 사용자마다 Extension을 별도로
생성할 필요는 없습니다.

**확인:** 대상 Quick 계정에 **Extension access 등록**과 **활성화된 Desktop Extension**이
모두 있어야 합니다. Enterprise 로그인이 설정되지 않았다는 오류가 발생하면 두 번째 항목이
누락되었거나 다른 Quick 계정에 생성되어 있는지 확인하세요.

## 6. Quick과 Cognito에 사용자 등록

한 사람이 활성화된 **Quick 사용자**와 **Cognito 사용자**를 모두 가지고 있어야 합니다.
두 사용자의 이메일 주소는 대소문자까지 정확하게 일치해야 합니다.

먼저 사용자가 대상 Quick 계정에 웹으로 로그인할 수 있는지 확인하세요. 필요하면 기존
사용자/그룹 관리 절차에 따라 Quick 사용자를 등록합니다. 사용할 기능에 필요한 Quick 역할과
구독이 부여되었는지도 확인합니다. Cognito 프로비저닝은 Quick 역할이나 기능 사용 권한을
부여하지 않습니다.

그런 다음 배포 모드에 맞는 방법으로 Cognito 사용자를 등록합니다.

### 모드 A — Cognito 사용자 직접 생성

1. AWS 콘솔의 `$AWS_REGION` 리전에서 **Amazon Cognito**를 엽니다.
2. **User pools**에서 ID가 `PoolId` 출력값과 일치하는 사용자 풀을 선택합니다.
   표시 이름은 `QuickDesktopUserPool`입니다.
3. **Users → Create user**를 선택합니다.
4. 사용자 이름은 `desktop-user-1`처럼 이메일 형식이 아닌 값을 사용합니다.
   **Email address**에는 사용자의 정확한 Quick 이메일을 입력합니다.
   이 풀은 이메일 별칭을 사용하므로 기본 사용자 이름에는 이메일 형식을 사용할 수 없습니다.
5. 조직의 자격 증명 정보에서 해당 이메일이 이 사용자의 주소임을 확인한 후 이메일을 검증됨으로
   표시합니다. 이메일 초대 전송과 임시 비밀번호 자동 생성을 선택합니다.
6. 사용자를 생성하고 **Your Amazon Quick Desktop invitation** 메일이 도착하는지 확인합니다.
   첫 로그인 전 상태는 `FORCE_CHANGE_PASSWORD`여야 합니다.
   콘솔에서는 비밀번호 변경이 필요한 상태로 표시됩니다.

<details>
<summary>같은 사용자 생성 단계를 CLI로 수행하는 방법</summary>

콘솔 대신 CLI로 사용자를 생성할 때 사용하세요. 예제 이메일은 활성 Quick 사용자의 검증된
이메일 주소로 바꿉니다. 2단계에서 설정한 터미널에서 실행하세요.

```bash
POOL_ID=$(aws cloudformation describe-stacks \
  --region "$AWS_REGION" \
  --stack-name QuickDesktopCognitoIdcStack \
  --query "Stacks[0].Outputs[?OutputKey=='PoolId'].OutputValue | [0]" \
  --output text)
QUICK_USER_EMAIL=person@example.com
COGNITO_USERNAME=desktop-user-1

aws cognito-idp admin-create-user \
  --region "$AWS_REGION" \
  --user-pool-id "$POOL_ID" \
  --username "$COGNITO_USERNAME" \
  --user-attributes "Name=email,Value=$QUICK_USER_EMAIL" Name=email_verified,Value=true \
  --desired-delivery-mediums EMAIL \
  --query 'User.{Username:Username,Status:UserStatus,Enabled:Enabled}' \
  --output table
```

Cognito가 임시 비밀번호를 생성하여 이메일로 발송합니다. 이 명령은 셸 기록에 비밀번호를
남기지 않습니다. `email_verified=true`는 관리자가 이메일 소유권을 확인했다는 의미이므로,
실제로 확인한 후에만 사용하세요.

</details>

### 모드 B — Identity Center에서 자동 프로비저닝

1. Identity Center에서 사용자가 `idcGroupName`으로 지정한 정확한 그룹에 속해 있는지
   확인합니다.
2. Quick 계정도 Identity Center를 사용한다면 사용자 할당 또는 그룹을 통해 해당 계정의
   적절한 Quick 역할이 부여되어 있는지 확인합니다.
3. Cognito에서 `PoolId`와 일치하는 사용자 풀의 **Users**를 확인합니다.
4. 초기 가져오기 또는 다음 동기화 작업을 기다립니다. 기본 주기는 5분이며,
   작업은 비동기로 처리됩니다.
5. 같은 이메일의 Cognito 사용자가 생성되고 초대 메일이 도착하는지 확인합니다.

Cognito의 기본 사용자 이름은 Identity Center 사용자 ID입니다. 초대 메일에서는 사용자가
**이메일 주소**로 로그인하도록 안내합니다. 스택은 관리 작업으로 디렉터리에서 가져온 이메일을
검증됨으로 표시합니다.

Identity Center 초대와 Cognito 초대는 별개입니다. Identity Center 비밀번호가 이미 있더라도
새 Cognito 비밀번호를 설정해야 합니다. 프로비저닝이 실패하면 같은 이메일의 Cognito 사용자를
수동으로 중복 생성하지 말고 [Identity Center 문제 해결](../TROUBLESHOOTING.md#iam-identity-center-provisioning)을
따르세요.

**확인:** 사용자가 Quick 웹에 로그인할 수 있고, 올바른 Cognito 풀에 동일한 검증된 이메일로
등록되어 있으며, Cognito 초대 메일을 받아야 합니다. 메일이 없다면 스팸함, 메일 격리 및
Cognito 이메일 전송 설정을 확인하세요.

### Cognito 초대 메일이 없는 경우

Cognito 사용자에 저장된 이메일 주소에서 **Your Amazon Quick Desktop invitation**을
찾으세요. 이 메일에는 별도의 Cognito 임시 비밀번호가 들어 있습니다. Identity Center
활성화 또는 비밀번호 재설정 메일로 대신할 수 없습니다. 동기화 로그의 `action=created`는
Cognito가 사용자 생성 요청을 수락했다는 의미이며, 메일함 도착을 보장하지는 않습니다.
동기화 작업은 이미 존재하는 사용자에게 초대를 반복해서 보내지 않습니다.

스팸함, 메일 격리 및 이메일 전송 설정을 확인한 후에도 메일이 없다면, 관리자는 아직
첫 로그인을 완료하지 않은 사용자에게 초대를 다시 보낼 수 있습니다. 풀의 Users 목록에서
**기존 Cognito 사용자 이름**을 복사하세요. Identity Center 모드에서는 디렉터리 사용자 ID입니다.
초대를 다시 보내면 새 임시 비밀번호가 생성되고 유효기간도 다시 시작되므로,
가장 최근 초대 메일을 사용하세요.

```bash
POOL_ID=$(aws cloudformation describe-stacks \
  --region "$AWS_REGION" \
  --stack-name QuickDesktopCognitoIdcStack \
  --query "Stacks[0].Outputs[?OutputKey=='PoolId'].OutputValue | [0]" \
  --output text)
COGNITO_USERNAME=REPLACE_WITH_EXISTING_COGNITO_USERNAME
COGNITO_STATUS=$(aws cognito-idp admin-get-user \
  --region "$AWS_REGION" --user-pool-id "$POOL_ID" \
  --username "$COGNITO_USERNAME" --query UserStatus --output text)

if [ "$COGNITO_STATUS" = "FORCE_CHANGE_PASSWORD" ]; then
  aws cognito-idp admin-create-user \
    --region "$AWS_REGION" --user-pool-id "$POOL_ID" \
    --username "$COGNITO_USERNAME" \
    --message-action RESEND --desired-delivery-mediums EMAIL \
    --query 'User.{Username:Username,Status:UserStatus}' --output table
else
  echo "No invitation resent. Check the user status; use password recovery after first sign-in."
fi
```

초대 메일을 받지 못했다는 이유로 같은 이메일의 사용자를 새로 생성하지 마세요.

## 7. Desktop 설치 및 첫 로그인

### 7.1 다운로드 및 설치

Quick의 **Extensions** 화면에서 활성 Desktop Extension의 **…** 메뉴를 열고
**Download for Mac** 또는 **Download for Windows**를 선택합니다.

[공식 Desktop 설치 안내](https://docs.aws.amazon.com/quick/latest/userguide/getting-started-desktop.html#desktop-download-install)에도
다음 직접 다운로드 링크가 제공됩니다.

- [macOS, Apple Silicon — Amazon-Quick.dmg](https://desktop.downloads.quick.aws.com/mac/arm64/Amazon-Quick.dmg)
- [Windows, x64 — Amazon-Quick.exe](https://desktop.downloads.quick.aws.com/windows/x64/Amazon-Quick.exe)

macOS에서는 DMG를 열고 Amazon Quick을 **Applications** 폴더로 옮깁니다.
Windows에서는 설치 프로그램을 실행하고 안내를 따릅니다.
회사 관리 기기에서는 IT 팀의 소프트웨어 설치 정책을 따르세요.

### 7.2 Enterprise 로그인 시작

1. 기기의 기본 브라우저에서 **테스트할 사용자로** 대상 Quick 계정에 로그인합니다.
   관리자 세션이나 다른 계정의 세션이 남아 있으면 잘못된 연동이 선택될 수 있습니다.
2. Amazon Quick Desktop을 실행합니다.
3. **Dynamic (recommended)**을 선택하거나 **Quick 계정의 홈 리전**을 직접 선택합니다.
4. **Continue with SSO**를 선택합니다.

<img src="images/desktop-sso.png" alt="Dynamic 리전과 Continue with SSO가 강조된 Desktop 로그인 화면" width="420">

브라우저는 먼저 웹 세션으로 Quick 계정을 확인합니다. Quick 로그인을 요청하면 해당 계정의
기존 로그인 방식으로 로그인하세요. 이후 Cognito 로그인 페이지로 이동합니다.
Desktop 인증 자체는 Cognito가 처리하지만, 그 전에 Quick 웹 로그인이 나타나는 것은
정상적인 흐름입니다.

Quick이 Identity Center를 사용한다면 이 초기 웹 로그인에는 Identity Center의 비밀번호와
MFA 정책이 적용됩니다. Cognito의 비밀번호와 MFA 설정은 별도입니다. 스택의 `mfaRequired`
옵션은 Cognito에만 적용되며 Identity Center의 MFA 정책을 변경하지 않습니다.
기본값은 Cognito TOTP MFA 필수이며, `mfaRequired=false`로 설정하면 선택 사항이 됩니다.

### 7.3 Cognito 비밀번호 및 MFA 설정

1. Cognito 페이지에 사용자의 **이메일 주소**와 **Cognito 초대 메일의 임시 비밀번호**를
   입력합니다.

   <img src="images/cognito-sign-in.png" alt="Cognito 호스팅 로그인 화면" width="420">

2. 비밀번호 변경 화면이 나오면 표시된 정책을 만족하는 새 비밀번호를 입력하고 확인합니다.

   <img src="images/cognito-new-password.png" alt="Cognito 첫 로그인 시 비밀번호 변경 화면" width="360">

3. 이 저장소의 기본 설정에서는 **인증 앱을 이용한 MFA**도 필수입니다.
   TOTP 설정 화면의 QR 코드 또는 설정 키를 사용자의 인증 앱에 등록하고,
   앱에 표시되는 현재 6자리 코드를 입력합니다. 설정 키와 복구 정보는 비공개로 보관하세요.
4. 나머지 안내를 완료하고 브라우저에 로그인 성공 메시지가 표시되면 Desktop으로 돌아갑니다.

![Amazon Quick 로그인 완료를 알리는 브라우저 화면](images/sign-in-complete.png)

원본 스크린샷에는 비밀번호 설정 화면만 포함되어 있습니다. 이 저장소의 기본 설정에서 필요한
추가 MFA 단계는 스크린샷에 표시되어 있지 않습니다. 다음 로그인부터는 새 Cognito 비밀번호와
인증 앱 코드를 사용합니다.

### 7.4 Desktop 세션 동작 확인

Desktop에 로그인된 홈 화면이 표시될 때까지 기다립니다.
올바른 사용자와 계정인지 확인한 후 다음과 같은 간단한 질문을 입력합니다.

> 우리 팀을 환영하는 메시지를 두 문장으로 작성해 줘.

답변이 표시되는지 확인하세요. 기본 로그인 확인에는 파일 접근 권한이나 커넥터 설정이
필요하지 않습니다. 첫 실행 시 나타나는 선택적 커넥터 설정은 필요에 따라 나중에 진행해도 됩니다.

![원본 가이드에서 Quick Desktop 대화가 정상 완료된 예시](images/desktop-chat.png)

**확인:** 애플리케이션에 로그인된 세션이 정상적으로 동작하고 질문에 답변할 수 있어야 합니다.
CloudFormation 배포 완료, Cognito 토큰 교환 성공, 브라우저의 로그인 성공 화면만으로
최종 검증이 끝난 것은 아닙니다.

## 8. 연동 검증 및 사용자 안내

나머지 사용자에게 안내하기 전에 다음을 확인하세요.

- [ ] CloudFormation 스택 두 개가 완료되었고 `CognitoReadiness`가 `READY`입니다.
- [ ] Quick Extension access의 다섯 값이 동일한 배포의 출력값과 일치합니다.
- [ ] 대상 Quick 계정에 별도의 Desktop Extension이 활성화되어 있습니다.
- [ ] 첫 사용자의 Quick 이메일과 Cognito 이메일이 정확하게 일치합니다.
- [ ] 사용자가 Cognito 초대 메일을 받고 비밀번호 변경과 MFA 등록을 완료했습니다.
- [ ] 새로 Desktop에 로그인하여 올바른 계정이 표시되고 대화에 답변이 돌아옵니다.
- [ ] 모드 B에서는 테스트 구성원이 프로비저닝되고, 그룹에서 제거하면 동기화 후 관리 대상
      Cognito 사용자가 비활성화됩니다. 기존 Quick 세션과 발급된 토큰의 유효기간은 별개입니다.
      [접근 권한 제거](CONFIGURATION.md#access-removal)를 참고하세요.

각 사용자에게 Quick 계정 로그인 방법, 공식 Desktop 다운로드 링크, 이 가이드의 사용자
단계 및 **본인 Cognito 초대 메일**을 안내하세요. 인증 앱도 미리 준비하도록 안내합니다.
공통 임시 비밀번호나 다른 사용자의 초대 메일을 공유하지 마세요.

AWS 계정과 리전, 배포한 버전, 선택한 CDK 옵션 및 출력값은 내부 운영 기록으로 보관합니다.
이 저장소는 `.local/`을 Git 추적 대상에서 제외합니다.
셸을 다시 열었다면 배포 업데이트나 삭제 전에 같은 자격 증명, 리전, `CDK_OPTIONS`를
다시 설정하세요.

### 확인 단계에서 문제가 발생한 경우

| 증상 | 다음 확인 사항 |
| --- | --- |
| CDK 명령에서 `idcGroupName is required` 발생 | 선택한 모드의 `CDK_OPTIONS` 배열을 다시 설정하고 bootstrap을 포함한 해당 명령에 전달하세요. |
| CDK에서 리전이 bootstrap되지 않았다고 표시 | 같은 AWS 계정과 컨텍스트로 인증 리전과 `us-east-1`에서 bootstrap을 수행하세요. |
| 동일한 이름의 리소스가 이미 존재 | 이전 스택이나 보존된 리소스를 확인하고 업그레이드/정리 안내를 검토한 후 새 스택을 생성하세요. |
| `ListInstances` 실패 또는 일치하는 인스턴스가 보이지 않음 | 모드 B의 배포 계정, 권한, 기본 리전 및 지정한 인스턴스 ARN을 확인하세요. |
| Extension access를 찾을 수 없거나 Extension 생성 불가 | Quick 관리자로 로그인하세요. IAM 전용 콘솔에 있다면 **Quick Sign In**을 사용합니다. |
| Enterprise 로그인이 설정되지 않았다고 표시 | Extension access뿐 아니라 **활성 Extension**이 있는지, 브라우저에서 대상 Quick 계정이 선택되어 있는지 확인하세요. |
| 사용자를 찾을 수 없음 | Quick 사용자가 활성 상태인지, Cognito 이메일이 대소문자까지 일치하는지 확인하세요. |
| `redirect_mismatch` | 배포된 `QuickDesktopAppClient`의 콜백을 확인하세요. 스택은 `http://localhost:18080`과 `http://127.0.0.1:18080`을 설정하며 끝에 슬래시를 추가하지 않습니다. |
| 초대 메일이 없거나 임시 비밀번호 만료 | 이메일 전송과 격리 상태를 확인하세요. 첫 로그인을 아직 완료하지 않은 사용자라면 Cognito 관리자에게 초대 메일 재전송을 요청합니다. |
| MFA 입력 화면 표시 | 기본 설정에서 TOTP MFA는 필수입니다. 사용자의 인증 앱을 등록하세요. |
| Cognito 로그인 후 토큰 또는 사용자 정보 오류 | Issuer, JWKS, 클라이언트 ID, 계정 선택, Extension 활성화를 다시 확인하고 [민감정보 없이 로그 연결하기](../TROUBLESHOOTING.md#correlate-customer-logs-without-collecting-tokens)를 참고하세요. |

더 자세한 진단은 [문제 해결 문서](../TROUBLESHOOTING.md)를 참고하세요.
Cognito 인증 오류와 이후 Quick 검증 오류를 구분하고, 비밀번호나 원본 토큰을 수집하지 않고
원인을 확인하는 방법을 안내합니다.

## 9. 테스트 배포 정리

테스트 연동에 속한 리소스만 정리하세요.

1. Quick에서 5단계에 생성한 Desktop Extension을 삭제합니다.
2. **Manage account → Permissions → Extension access**에서 4단계의 해당 접근 설정을
   삭제합니다.
3. 저장소 디렉터리에서 원래 자격 증명, 리전, `CDK_OPTIONS`를 다시 설정한 후
   다음 명령을 검토하고 실행합니다.

   ```bash
   npx cdk destroy --all "${CDK_OPTIONS[@]}"
   ```

기본 설정에서는 Cognito 사용자 풀과 선택적으로 생성된 CloudTrail 로그 버킷이
**보존**됩니다. 원본 Identity Center 그룹도 그대로 남습니다.
보존된 리소스는 계속 비용이 발생하거나 새 배포와 이름이 충돌할 수 있으므로 별도로 검토하세요.
이 정리 과정에서 공유 디렉터리, Quick 구독 또는 CDK bootstrap 스택을 삭제하지 마세요.

보존된 로그와 공유 API Gateway 설정에 대해서는 [정리 세부 안내](CONFIGURATION.md#cleanup)를
참고하세요.

## 참고 자료

- [BK (Byeonghun) Kim의 원본 한국어 그림 가이드](https://madebybk.notion.site/amazon-quick-desktop-enterprise-idp-cognito-idc)
- [AWS: Enterprise Desktop 설정, 사용자 매핑 및 계정 사전 요구 사항](https://docs.aws.amazon.com/quick/latest/userguide/desktop-enterprise-setup.html)
- [AWS: Extension access, Extension 생성 및 Desktop 다운로드](https://docs.aws.amazon.com/quick/latest/userguide/desktop-enterprise-entra-id.html)
- [AWS: Desktop 설치 및 로그인](https://docs.aws.amazon.com/quick/latest/userguide/getting-started-desktop.html)
- [AWS: Enterprise 로그인 문제 해결](https://docs.aws.amazon.com/quick/latest/userguide/desktop-enterprise-setup-troubleshooting.html)

영문판의 콘솔 절차는 2026년 9월 22일 AWS 문서를 기준으로 확인했으며,
이 한국어판은 같은 절차와 명령어를 제공합니다. 연결된 IdP별 문서는 공통 Quick 측 설정을
참고하기 위한 것이며, 이 샘플의 CDK 스택이 Cognito 클라이언트와 엔드포인트를 제공합니다.
