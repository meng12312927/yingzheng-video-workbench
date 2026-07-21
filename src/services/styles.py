"""受控样式目录：LLM 只能推荐这里已经可渲染的组合。"""

from __future__ import annotations

from src.models.schemas import StyleAsset, StyleBundle


class StyleCatalog:
    def __init__(self):
        self.assets = {
            asset.id: asset
            for asset in [
                StyleAsset(id="intro_none", category="intro", name="无片头", description="直接进入正文", config={"intro_style": "none"}),
                StyleAsset(id="intro_title", category="intro", name="标题片头", description="2 秒活动标题卡", config={"intro_style": "title"}),
                StyleAsset(id="intro_fade", category="intro", name="黑场淡入", description="2 秒黑场淡入", config={"intro_style": "fade_black"}),
                StyleAsset(id="outro_none", category="outro", name="无片尾", description="正文结束即完成", config={"outro_style": "none"}),
                StyleAsset(id="outro_title", category="outro", name="感谢观看", description="2 秒感谢观看片尾", config={"outro_style": "title"}),
                StyleAsset(id="outro_fade", category="outro", name="黑场淡出", description="2 秒黑场淡出", config={"outro_style": "fade_black"}),
                StyleAsset(id="subtitle_classic", category="subtitle", name="经典字幕", description="通用白字描边", config={"subtitle_style": "classic"}),
                StyleAsset(id="subtitle_clean", category="subtitle", name="简洁字幕", description="正式简洁白字", config={"subtitle_style": "clean"}),
                StyleAsset(id="subtitle_highlight", category="subtitle", name="醒目字幕", description="适合热烈活动的黄色字幕", config={"subtitle_style": "highlight"}),
                StyleAsset(id="transition_cut", category="transition", name="硬切", description="节奏直接，不增加重叠", config={"transition_duration": 0.0}),
                StyleAsset(id="transition_fade", category="transition", name="淡转场", description="0.35 秒淡转场", config={"transition_duration": 0.35}),
            ]
        }
        self.bundles = {
            bundle.id: bundle
            for bundle in [
                StyleBundle(
                    id="bundle_formal_clean",
                    name="正式简洁",
                    scenario="common",
                    intro_style_id="intro_title",
                    outro_style_id="outro_fade",
                    subtitle_style_id="subtitle_clean",
                    transition_style_id="transition_fade",
                ),
                StyleBundle(
                    id="bundle_school_highlight",
                    name="校园高光",
                    scenario="school",
                    intro_style_id="intro_title",
                    outro_style_id="outro_title",
                    subtitle_style_id="subtitle_highlight",
                    transition_style_id="transition_fade",
                ),
                StyleBundle(
                    id="bundle_enterprise_report",
                    name="企业纪要",
                    scenario="enterprise",
                    intro_style_id="intro_fade",
                    outro_style_id="outro_fade",
                    subtitle_style_id="subtitle_clean",
                    transition_style_id="transition_cut",
                ),
            ]
        }
        self._validate()

    def available_bundles(self, scenario: str) -> list[StyleBundle]:
        return [
            bundle for bundle in self.bundles.values()
            if bundle.scenario in {"common", scenario}
        ]

    def bundle_config(self, bundle_id: str) -> dict:
        bundle = self.bundles.get(bundle_id)
        if bundle is None:
            raise ValueError("未知样式组合")
        config = {}
        for asset_id in (
            bundle.intro_style_id,
            bundle.outro_style_id,
            bundle.subtitle_style_id,
            bundle.transition_style_id,
        ):
            config.update(self.assets[asset_id].config)
        return config

    def _validate(self) -> None:
        for bundle in self.bundles.values():
            references = {
                "intro": bundle.intro_style_id,
                "outro": bundle.outro_style_id,
                "subtitle": bundle.subtitle_style_id,
                "transition": bundle.transition_style_id,
            }
            for category, asset_id in references.items():
                asset = self.assets.get(asset_id)
                if asset is None or asset.category != category or not asset.active:
                    raise ValueError(f"样式组合 {bundle.id} 引用了非法 {category} 资源")

