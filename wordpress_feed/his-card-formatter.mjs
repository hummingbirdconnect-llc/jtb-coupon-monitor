const DISCOUNT_PATTERN =
  /(?:最大|合計)?[\d,]+(?:円(?:引き|割引|助成)|[％%]引き)(?:（[^）]{1,40}）)?/;

function cleanText(value) {
  return String(value || "")
    .replace(/\s+/g, " ")
    .trim();
}

function unique(values) {
  return [...new Set(values.filter(Boolean))];
}

function truncateText(value, limit) {
  const characters = Array.from(value);
  if (characters.length <= limit) {
    return value;
  }
  return `${characters.slice(0, Math.max(1, limit - 1)).join("")}…`;
}

function normalizeDiscountText(value) {
  return cleanText(value)
    .replaceAll("%", "％")
    .replace(/円引(?!き)/g, "円引き");
}

function extractDiscount(value) {
  const source = normalizeDiscountText(value);
  const match = source.match(DISCOUNT_PATTERN);
  if (!match) {
    return null;
  }
  return {
    index: match.index,
    label: normalizeDiscountText(match[0]),
  };
}

function discountNumericValue(value) {
  const match = normalizeDiscountText(value).match(/[\d,]+/);
  return match ? Number.parseInt(match[0].replaceAll(",", ""), 10) : 0;
}

function headlineDiscount(coupon) {
  const titleMatch = extractDiscount(coupon.title);
  if (titleMatch) {
    return titleMatch.label.replace(/^合計/, "");
  }

  const directMatch = extractDiscount(coupon.discount);
  if (directMatch) {
    return directMatch.label.replace(/^合計/, "");
  }

  const codeDiscounts = (Array.isArray(coupon.coupon_codes)
    ? coupon.coupon_codes
    : []
  )
    .map((code) => extractDiscount(code?.discount)?.label || "")
    .filter(Boolean)
    .sort((left, right) => discountNumericValue(right) - discountNumericValue(left));

  return (codeDiscounts[0] || "").replace(/^合計/, "");
}

function extractRequirements(value) {
  const source = cleanText(value);
  const patterns = [
    /(\d+名様以上のグループ)で利用可/g,
    /(1名様の旅行代金[\d,]+円以上)/g,
    /(宿泊代金総額[\d,]+円以上)/g,
  ];
  const requirements = [];

  for (const pattern of patterns) {
    for (const match of source.matchAll(pattern)) {
      requirements.push(cleanText(match[1]));
    }
  }

  return unique(requirements);
}

function compactCodeCondition(value) {
  return cleanText(value)
    .replace(/出発の(\d+)日以上前まで/gu, "$1日前まで")
    .replace(/1名様の旅行代金/gu, "")
    .replace(/宿泊代金総額/gu, "宿泊代金")
    .replace(/([\d,]+)円/gu, (match, amount) => {
      const numeric = Number.parseInt(amount.replaceAll(",", ""), 10);
      return numeric > 0 && numeric % 10000 === 0
        ? `${numeric / 10000}万円`
        : match;
    })
    .replace(/で利用可$/u, "")
    .trim();
}

function compactCodeDiscount(value) {
  return cleanText(value).replace(/([\d,]+)円/gu, (match, amount) => {
    const numeric = Number.parseInt(amount.replaceAll(",", ""), 10);
    if (numeric > 0 && numeric % 10000 === 0) {
      return `${numeric / 10000}万円`;
    }
    if (numeric > 0 && numeric < 10000 && numeric % 1000 === 0) {
      return `${numeric / 1000}千円`;
    }
    return match;
  });
}

function formatCodes(codes) {
  const parsed = (Array.isArray(codes) ? codes : [])
    .filter((code) => code && cleanText(code.code))
    .map((code) => ({
      code: cleanText(code.code),
      condition: cleanText(code.condition),
      discount: extractDiscount(code.discount)?.label || cleanText(code.discount),
      requirements: extractRequirements(code.discount),
    }));

  const commonRequirements =
    parsed.length > 0
      ? parsed[0].requirements.filter((requirement) =>
          parsed.every((code) => code.requirements.includes(requirement)),
        )
      : [];
  const commonOnlineOnly =
    parsed.length > 0 &&
    parsed.every((code) => code.condition === "オンライン予約限定");

  return {
    codes: parsed.map((code) => {
      const codeOnlyRequirements = code.requirements.filter(
        (requirement) => !commonRequirements.includes(requirement),
      );
      return {
        code: code.code,
        condition: unique([
          commonOnlineOnly ? "" : code.condition,
          ...codeOnlyRequirements,
        ])
          .map(compactCodeCondition)
          .filter(Boolean)
          .join("・"),
        discount: compactCodeDiscount(code.discount),
      };
    }),
    commonRequirements,
  };
}

function titleCore(title) {
  const source = cleanText(title);
  const discount = extractDiscount(source);
  let core = discount ? source.slice(0, discount.index) : source;

  core = core
    .replace(/(?:1グループ|1名様につき|お1人様|利用プラン)\s*$/u, "")
    .replace(/^オンライン予約限定[！!]\s*/u, "")
    .replace(/^早めの予約がお得[！!]\s*/u, "早期割 ")
    .replace(/^夏旅応援[！!]\s*/u, "")
    .replace(/^まだ間に合う[！!]\s*/u, "")
    .replace(/^夏休みも連休もお得[！!]\s*/u, "")
    .replace(/^期間中に/u, "")
    .replace(/^ナイトタイムクーポン[！!]\s*/u, "ナイトタイム ")
    .replace(/(?:(?:で|に)使える|がお得|ならお得)[！!]?\s*$/u, "")
    .replace(/をお申し込みで\s*$/u, "")
    .replace(/をお申込みで\s*$/u, "")
    .replace(/[！!]+/gu, " ")
    .replace(/\s+/g, " ")
    .trim();

  if (/^早期割/u.test(core)) {
    return "早期割";
  }

  return core;
}

function sharesProductTerm(category, core) {
  const productTerms = [
    "航空券",
    "ホテル",
    "沖縄",
    "eSIM",
    "アイスランド",
    "鉄道",
    "新幹線",
    "バス",
    "グランピング",
    "コテージ",
    "別荘",
    "変なホテル",
    "辻のや",
    "佐賀",
    "空港",
  ];
  return productTerms.some(
    (term) => category.includes(term) && core.includes(term),
  );
}

function couponName(coupon) {
  const category = cleanText(coupon.category) || "HIS";
  const core = titleCore(coupon.title);
  let name;

  if (!core) {
    name = category;
  } else if (core.includes(category) || sharesProductTerm(category, core)) {
    name = core;
  } else {
    name = `${category} ${core}`;
  }

  name = name
    .replace(/\s*クーポン\s*クーポン$/u, "クーポン")
    .replace(/\s+/g, " ")
    .trim();

  if (!name.endsWith("クーポン")) {
    name = `${name}クーポン`;
  }

  return truncateText(name, 64);
}

function positiveTarget(value) {
  return cleanText(String(value || "").split("【対象外】")[0])
    .replace(/^HISが(?:旅行)?企画・実施する/u, "")
    .replace(/\s*※.*$/u, "")
    .replace(/[。．]\s*$/u, "")
    .trim();
}

function compactEnumeration(value) {
  return String(value || "").replace(
    /[A-Z]+\d{3,6}(?:、[A-Z]+\d{3,6}){3,}/gu,
    (list) => {
      const codes = list.split("、");
      return `${codes[0]}ほか計${codes.length}件`;
    },
  );
}

function targetIsRedundant(target, category) {
  if (!target || !target.includes(category)) {
    return false;
  }
  const remainder = target
    .replace(category, "")
    .replace(/（[^）]*）/gu, "")
    .replace(/[・、。\s]/gu, "");
  return Array.from(remainder).length <= 8;
}

function requirementSentence(requirement) {
  if (requirement.endsWith("グループ")) {
    return `${requirement}が対象です。`;
  }
  return `${requirement}が条件です。`;
}

function note(coupon, commonRequirements) {
  const category = cleanText(coupon.category);
  const target = compactEnumeration(positiveTarget(coupon.target));
  const title = cleanText(coupon.title);
  const sourceNotes = (Array.isArray(coupon.notes) ? coupon.notes : [])
    .map(cleanText)
    .join(" ");
  const codeConditions = (Array.isArray(coupon.coupon_codes)
    ? coupon.coupon_codes
    : []
  )
    .map((code) => cleanText(code?.condition))
    .join(" ");
  const codeDiscounts = (Array.isArray(coupon.coupon_codes)
    ? coupon.coupon_codes
    : []
  )
    .map((code) => cleanText(code?.discount))
    .filter(Boolean);
  const sentences = [];

  if (target && !targetIsRedundant(target, category)) {
    sentences.push(
      target.includes("対象") ? `${target}です。` : `${target}が対象です。`,
    );
  }

  sentences.push(...commonRequirements.map(requirementSentence));
  if (
    codeDiscounts.length > 1 &&
    codeDiscounts.every((value) => /1名様の旅行代金/u.test(value)) &&
    !commonRequirements.some((value) => /1名様の旅行代金/u.test(value))
  ) {
    sentences.push("金額条件は1名あたりです。");
  }

  const onlineOnly =
    /オンライン予約限定/u.test(`${title} ${codeConditions} ${sourceNotes}`) ||
    /(?:予約サイト|公式サイト)から(?:の)?ご予約の場合のみ/u.test(
      `${coupon.target || ""} ${sourceNotes}`,
    );
  const onlineAvailable = /お支払い画面|クーポン利用画面/u.test(sourceNotes);

  if (onlineOnly) {
    sentences.push("オンライン予約限定です。");
  } else if (onlineAvailable) {
    sentences.push("オンライン予約でも利用できます。");
  }

  const compact = [];
  for (const sentence of unique(sentences)) {
    if (Array.from(compact.join("") + sentence).length > 180) {
      continue;
    }
    compact.push(sentence);
    if (compact.length >= 3) {
      break;
    }
  }

  return compact.join("") || "詳細条件はリンク先でご確認ください。";
}

function discountLabelWithUnit(coupon, discountLabel) {
  if (!discountLabel) {
    return discountLabel;
  }

  const sourceTitle = cleanText(coupon.title);
  if (/(?:1名様につき|お1人様)/u.test(sourceTitle)) {
    return `1名${discountLabel}`;
  }
  if (/1グループ/u.test(sourceTitle)) {
    return `1組${discountLabel}`;
  }

  return discountLabel;
}

export function formatHisCouponForCard(coupon) {
  const category = cleanText(coupon.category) || "HIS";
  const formattedCodes = formatCodes(coupon.coupon_codes);
  const formattedCouponName = couponName(coupon);
  const formattedNote = note(coupon, formattedCodes.commonRequirements);
  const discountLabel = discountLabelWithUnit(
    coupon,
    headlineDiscount(coupon),
  );
  const displayTitle = discountLabel
    ? `${category} ${discountLabel}クーポンを確認する`
    : `${formattedCouponName}を確認する`;

  return {
    couponName: formattedCouponName,
    discountLabel,
    displayTitle: truncateText(displayTitle, 72),
    codes: formattedCodes.codes,
    note: formattedNote,
    buttonText: "クーポンを確認する",
    sourceCategory: category,
    sourceTitle: cleanText(coupon.title),
    presentationVersion: 2,
  };
}
