import random
import tempfile
import unittest
from pathlib import Path

import fitz

from label_sorter import sorter


class PdfMergeTests(unittest.TestCase):
    def test_sort_preserves_shared_image_resource(self):
        with tempfile.TemporaryDirectory() as tmp:
            source_path = Path(tmp) / "source.pdf"
            output_path = Path(tmp) / "sorted.pdf"
            self._make_shared_image_pdf(source_path, (6, 1, 5, 2, 4, 3))

            stats = sorter.sort_and_merge(
                [str(source_path)],
                str(output_path),
                fallback="none",
                threads=1,
                recheck=False,
            )

            self.assertEqual(stats["total"], 6)
            self.assertLess(output_path.stat().st_size,
                            source_path.stat().st_size * 1.25)

            output = fitz.open(output_path)
            try:
                skus = [page.get_text().strip() for page in output]
                image_xrefs = [
                    image[0]
                    for page in output
                    for image in page.get_images(full=True)
                ]
            finally:
                output.close()

            self.assertEqual(skus, [f"HD-{i:03d}" for i in range(1, 7)])
            self.assertEqual(len(image_xrefs), 6)
            self.assertEqual(len(set(image_xrefs)), 1)

    def test_interleaved_sources_keep_separate_graft_maps(self):
        with tempfile.TemporaryDirectory() as tmp:
            first_path = Path(tmp) / "first.pdf"
            second_path = Path(tmp) / "second.pdf"
            output_path = Path(tmp) / "sorted.pdf"
            self._make_shared_image_pdf(first_path, (6, 4, 2), seed=1)
            self._make_shared_image_pdf(second_path, (5, 3, 1), seed=2)

            sorter.sort_and_merge(
                [str(first_path), str(second_path)],
                str(output_path),
                fallback="none",
                threads=1,
                recheck=False,
            )

            output = fitz.open(output_path)
            try:
                skus = [page.get_text().strip() for page in output]
                image_xrefs = [page.get_images(full=True)[0][0]
                               for page in output]
            finally:
                output.close()

            self.assertEqual(skus, [f"HD-{i:03d}" for i in range(1, 7)])
            self.assertEqual(len(set(image_xrefs)), 2)
            self.assertEqual(image_xrefs[0::2], [image_xrefs[0]] * 3)
            self.assertEqual(image_xrefs[1::2], [image_xrefs[1]] * 3)

    @staticmethod
    def _make_shared_image_pdf(path, sku_numbers, seed=0):
        width = height = 400
        samples = random.Random(seed).randbytes(width * height * 3)
        pixmap = fitz.Pixmap(fitz.csRGB, width, height, samples, False)
        doc = fitz.open()
        image_xref = 0
        try:
            for sku_number in sku_numbers:
                page = doc.new_page(width=612, height=792)
                image_xref = page.insert_image(
                    fitz.Rect(40, 40, 440, 440),
                    pixmap=pixmap,
                    xref=image_xref,
                )
                page.insert_text((40, 500), f"HD-{sku_number:03d}")
            doc.save(path, garbage=4, deflate=True, use_objstms=1)
        finally:
            doc.close()


if __name__ == "__main__":
    unittest.main()
