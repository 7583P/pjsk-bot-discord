import io
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from PIL import Image
from cogs.matchmaking import Top100View
from helpers.leaderboard import equipped_title, generate_top100_image, rank_gradient

class Titles(unittest.TestCase):
    def test_all_rank_gradients_match_the_website_including_direction(self):
        import re
        from pathlib import Path
        from cogs.matchmaking import RANK_ROLE_IDS
        css=(Path(__file__).resolve().parents[1]/'web/index.html').read_text(encoding='utf-8')
        for role in RANK_ROLE_IDS:
            key=role.replace(chr(11014),'Up').replace(' ','')
            rule=re.search(r'\.rank-'+key+r'\s*\{([^}]+)\}',css).group(1)
            gradient=re.search(r'linear-gradient\(to (left|right),\s*(#[0-9a-fA-F]{6}),\s*(#[0-9a-fA-F]{6})\)',rule)
            direction,first,second=gradient.groups()
            expected=(second.upper(),first.upper()) if direction=='left' else (first.upper(),second.upper())
            self.assertEqual(rank_gradient(role),expected,role)
        self.assertEqual(rank_gradient('Season Placement'),('#FF8519','#FF8519'))

    def test_selected_title_and_stale_current_selection(self):
        self.assertEqual(equipped_title({"effective_title_key":"current:8:place:1"}), (None,None))
        self.assertEqual(equipped_title({"effective_title_key":"current:8:place:1", "title_label":"CURRENT Season 5 1st Place","title_style":"gold"}), ("CURRENT Season 5 1st Place","gold"))
        self.assertEqual(equipped_title({"effective_title_key":"season:7:place:2","title_season_name":"Season 4"}), ("Season 4 2nd Place","silver"))
        self.assertEqual(equipped_title({}), (None,None))

    def test_empty_season_renders_valid_portrait(self):
        with Image.open(generate_top100_image([],"Empty season")) as image:
            self.assertEqual(image.size,(1024,1536))
            self.assertEqual(image.format,"PNG")

class Pagination(unittest.IsolatedAsyncioTestCase):
    async def test_discord_button_payload_has_text_and_no_invalid_emoji(self):
        view=Top100View(None,None,SimpleNamespace(guild=None),8,"Season 5 (New Era)")
        components=view.to_components()[0]['components']
        self.assertEqual([item['label'] for item in components],['Anterior','Siguiente'])
        for item in components:
            self.assertNotIn('emoji',item)
            self.assertEqual(item['type'],2)
            self.assertTrue(item['custom_id'])
        view.stop()

    async def test_all_100_standings_cached_and_attachment_replaced_on_navigation(self):
        standings=[dict(user_id=i,member_name=f"Player {i}",role="Master",visible_mmr=900-i,is_unplayed_sp=False) for i in range(1,106)]
        cog=SimpleNamespace(_build_visible_leaderboard_rows=AsyncMock(return_value=standings))
        fetch=AsyncMock(return_value=[dict(user_id=2,effective_title_key="current:8:place:2",title_label="CURRENT Season 5 2nd Place",title_style="silver")])
        class Pool:
            @asynccontextmanager
            async def acquire(self): yield SimpleNamespace(fetch=fetch)
        initial=SimpleNamespace(guild=SimpleNamespace())
        view=Top100View(cog,Pool(),initial,8,"Season 5 (New Era)")
        rows=await view._get_rows()
        self.assertEqual([row['user_id'] for row in rows],list(range(1,101)))
        self.assertEqual(rows[1]['title_label'],"CURRENT Season 5 2nd Place")
        self.assertIsNone(rows[0]['title_label'])
        embed,first=await view.build_payload()
        first_bytes=first.fp.getvalue()
        self.assertTrue(view.prev_page.disabled)
        self.assertFalse(view.next_page.disabled)
        interaction=SimpleNamespace(response=SimpleNamespace(defer=AsyncMock()),edit_original_response=AsyncMock())
        await view.next_page.callback(interaction)
        kwargs=interaction.edit_original_response.call_args.kwargs
        self.assertEqual(len(kwargs['attachments']),1)
        self.assertEqual(kwargs['attachments'][0].filename,'top100-8-2.png')
        self.assertEqual(view.page,1)
        await view.prev_page.callback(interaction)
        self.assertEqual(interaction.edit_original_response.call_args.kwargs['attachments'][0].fp.getvalue(),first_bytes)
        view.page=999
        embed,last=await view.build_payload()
        self.assertEqual(view.page,9)
        self.assertTrue(view.next_page.disabled)
        self.assertEqual(last.filename,'top100-8-10.png')
        cog._build_visible_leaderboard_rows.assert_awaited_once()
        fetch.assert_awaited_once()
        self.assertEqual(len(view._image_cache),3)
        view.stop()

if __name__=='__main__':unittest.main()
