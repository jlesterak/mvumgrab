import unittest

from mvum.server import fkey


class ForestKey(unittest.TestCase):
    def test_catalog_and_vector_names_join(self):
        """fs.usda.gov and EDW spell combined units differently (2026-11-25: 7 forests failed to join)."""
        pairs = [("Arapaho and Roosevelt National Forests & Pawnee National Grassland", "Arapaho and Roosevelt National Forests"),
                 ("Cibola National Forest and National Grasslands", "Cibola National Forest"),
                 ("Mount Baker-Snoqualmie National Forest", "Mt. Baker-Snoqualmie National Forest"),
                 ("Pike-San Isabel National Forests & Cimarron and Comanche National Grasslands", "Pike and San Isabel National Forests"),
                 ("Rogue River-Siskiyou National Forest", "Rogue River-Siskiyou National Forests")]
        for site, edw in pairs:
            self.assertEqual(fkey(site), fkey(edw), (site, edw))
        self.assertNotEqual(fkey("Custer National Forest"), fkey("Custer Gallatin National Forest"))


if __name__ == "__main__":
    unittest.main()
