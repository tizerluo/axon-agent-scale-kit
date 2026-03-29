"""
scripts/_shared_crypto.py

Keeper 链上验证使用的加密原语，与 Axon keeper 源码保持一致。
所有使用 keeper 哈希算法的代码应从此模块导入，不要自行实现。

当前实现的函数：
"""

import hashlib


def go_normalize(s: str) -> str:
    """
    等价于 keeper 中的 normalizeAnswer()。
    去掉所有空格、Tab、换行，小写。
    与 _go_normalize 的区别：接受任意 unicode 字符（非 ASCII 也保留）。
    """
    result = []
    for c in s:
        if 'A' <= c <= 'Z':
            result.append(chr(ord(c) + 32))
        elif c not in (' ', '\t', '\n', '\r'):
            result.append(c)
    return ''.join(result)


def keeper_answer_hash(answer: str) -> str:
    """
    AnswerHash（challengePool 中的 hash）使用的算法。
    keeper 在 evaluate 时：revealHash = SHA256(normalizeAnswer(resp.RevealData))
    normalizeAnswer 去掉所有空格/Tab/换行，转小写。
    """
    return hashlib.sha256(go_normalize(answer).encode("utf-8")).hexdigest()


def keeper_question_hash(question: str) -> str:
    """
    Keeper GenerateChallenge 中使用的 ChallengeHash。
    算法：SHA256(question text)
    用于通过链上返回的 challenge_hash 逆向查找对应的 question。
    keeper 暴露的 challenge_data 即为此值。
    """
    return hashlib.sha256(question.encode("utf-8")).hexdigest()


def keeper_commit_hash(cosmos_address: str, answer: str) -> str:
    """
    Keeper 在 reveal 时验证的 commit hash。
    算法：SHA256(bech32_addr + ":" + raw_answer)
    注意：不做任何 normalize，直接对原始 bytes 做 SHA256。
    """
    return hashlib.sha256(f"{cosmos_address}:{answer}".encode("utf-8")).hexdigest()
