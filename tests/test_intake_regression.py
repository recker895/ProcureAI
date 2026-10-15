import unittest
import document_intake as intake
import domain

class ExtractionRegression(unittest.TestCase):
    def test_validated_fields_not_missing(self):
        result=intake.validate_proposal({'fields':{'quantity':300},'sources':{'quantity':'Quantity: 300'},'missing':['quantity','supplier']},'Quantity: 300','quotation')
        self.assertEqual(result['missing'],['supplier'])
    def test_optional_empty_certifications_do_not_claim_evidence(self):
        result=intake.validate_proposal({'fields':{'certifications':[]},'sources':{},'missing':[]},'Supplier: TEST','quotation')
        self.assertNotIn('certifications',result['fields'])
        self.assertIn('certifications',result['missing'])
    def test_claimed_certification_still_requires_source(self):
        with self.assertRaises(domain.RuleError):
            intake.validate_proposal({'fields':{'certifications':['ISO 9001']},'sources':{},'missing':[]},'Supplier: TEST','quotation')
