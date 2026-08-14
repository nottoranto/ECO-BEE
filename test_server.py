import json, os, tempfile, threading, unittest, urllib.error, urllib.request
from http.server import ThreadingHTTPServer

tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ["ECOBEE_DB"] = tmp.name
os.environ["ECOBEE_ADMIN_PASSWORD"] = "test-admin-password"
import server


class BackendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server.init_db()
        cls.httpd=ThreadingHTTPServer(("127.0.0.1",0),server.API)
        cls.base=f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        cls.thread=threading.Thread(target=cls.httpd.serve_forever,daemon=True); cls.thread.start()
    @classmethod
    def tearDownClass(cls): cls.httpd.shutdown(); cls.httpd.server_close(); os.unlink(tmp.name)

    @classmethod
    def request(cls, method, path, payload=None, token=None):
        headers={"Content-Type":"application/json"}
        if token: headers["Authorization"]="Bearer "+token
        req=urllib.request.Request(cls.base+path,data=json.dumps(payload).encode() if payload is not None else None,headers=headers,method=method)
        try:
            with urllib.request.urlopen(req) as response:return response.status,json.load(response)
        except urllib.error.HTTPError as error:return error.code,json.load(error)

    def test_password_hash(self):
        value=server.hash_password("secret")
        self.assertTrue(server.verify_password("secret",value))
        self.assertFalse(server.verify_password("wrong",value))

    def test_distance(self):
        self.assertAlmostEqual(server.haversine(13.5282,99.8134,13.5282,99.8134),0)
        self.assertGreater(server.haversine(13.5282,99.8134,13.5382,99.8134),1)

    def test_geometry_center(self):
        self.assertEqual(server.geometry_center({"type":"point","coords":[13,99]}),(13.0,99.0))
        self.assertEqual(server.geometry_center({"type":"polygon","coords":[[10,20],[12,24]]}),(11,22))

    def test_postgres_adapter_escapes_literal_percent(self):
        class FakeRaw:
            def execute(self, sql, params):
                return sql, params
        adapter=server.PostgresConnection.__new__(server.PostgresConnection)
        adapter.raw=FakeRaw()
        sql,params=adapter.execute("SELECT 1 WHERE key LIKE 'myHives_%' AND scope=?",("private",))
        self.assertEqual(sql,"SELECT 1 WHERE key LIKE 'myHives_%%' AND scope=%s")
        self.assertEqual(params,("private",))

    def test_complete_trace_workflow(self):
        _,auth=self.request("POST","/api/auth/register",{"phone":"0899999999","password":"safe-pass","name":"ผู้ทดสอบ","farm":"ฟาร์มทดสอบ"})
        token=auth["token"]
        _,hive=self.request("POST","/api/hives",{"name":"รังทดสอบ","species":"cerana","lat":13.5,"lng":99.8},token)
        _,batch=self.request("POST","/api/harvests",{"hive_id":hive["id"],"product":"น้ำผึ้ง","quantity_kg":1.5},token)
        _,trace=self.request("GET","/api/trace/"+batch["batch_code"])
        self.assertEqual(trace["farm"],"ฟาร์มทดสอบ")
        self.assertEqual(trace["hive_name"],"รังทดสอบ")
        serialized=json.dumps(trace)
        for secret in ('"lat"','"lng"','from_lat','from_lng','to_lat','to_lng','hive_id'):
            self.assertNotIn(secret,serialized)
        self.assertIn("environment",trace)

    def test_hive_keeps_farmer_selected_foraging_radius(self):
        _,auth=self.request("POST","/api/auth/register",{"phone":"0898888888","password":"safe-pass","name":"ผู้ทดสอบรัศมี","farm":"ฟาร์มรัศมี"})
        status,hive=self.request("POST","/api/hives",{"name":"รังรัศมี","species":"meliponini","lat":13.5,"lng":99.8,"radius_km":1.2},auth["token"])
        self.assertEqual(status,201)
        self.assertEqual(hive["radius_km"],1.2)
        _,mapped=self.request("GET","/api/map-data",token=auth["token"])
        saved=next(x for x in mapped["hives"] if x["id"]==hive["id"])
        self.assertEqual(saved["radius_km"],1.2)

    def test_public_trace_uses_real_aggregates_without_coordinates(self):
        _,auth=self.request("POST","/api/auth/register",{"phone":"0891111111","password":"safe-pass","name":"ผู้ทดสอบสิ่งแวดล้อม","farm":"ฟาร์มข้อมูลจริง"})
        token=auth["token"]
        _,hive=self.request("POST","/api/hives",{"name":"รังข้อมูลจริง","species":"cerana","lat":13.5,"lng":99.8},token)
        self.request("POST","/api/plants",{"plant_type":"longan","variety":"อีดอ","months":[1,2,3],"geometry":{"type":"point","coords":[13.501,99.801]}},token)
        self.request("POST","/api/risk-zones",{"name":"พื้นที่เสี่ยงจริง","status":"danger","geometry":{"type":"point","coords":[13.502,99.802]}},token)
        self.request("POST","/api/movements",{"hive_id":hive["id"],"lat":13.503,"lng":99.803,"reason":"ย้ายตามฤดู"},token)
        _,batch=self.request("POST","/api/harvests",{"hive_id":hive["id"],"product":"น้ำผึ้งลำไย","quantity_kg":2},token)
        _,trace=self.request("GET","/api/trace/"+batch["batch_code"])
        self.assertEqual(trace["environment"]["plants"][0]["type"],"longan")
        self.assertEqual(trace["environment"]["plants"][0]["count"],1)
        self.assertEqual(trace["environment"]["danger_zone_count"],1)
        self.assertEqual(trace["environment"]["food_months"],[1,2,3])
        self.assertEqual(trace["movements"],[{"reason":"ย้ายตามฤดู","checked_in_at":trace["movements"][0]["checked_in_at"]}])

    def test_organization_login(self):
        status,data=self.request("POST","/api/org/auth/login",{"email":"admin@ecobee.go.th","password":"test-admin-password"})
        self.assertEqual(status,200); self.assertTrue(data["token"])
        status,farmers=self.request("GET","/api/org/farmers",token=data["token"])
        self.assertEqual(status,200); self.assertIsInstance(farmers,list)

    def test_storage_requires_auth_and_enforces_owner(self):
        status,_=self.request("GET","/api/storage/private/accounts")
        self.assertEqual(status,403)
        _,auth=self.request("POST","/api/auth/register",{"phone":"0888888888","password":"strong-pass","name":"เจ้าของข้อมูล","farm":"ฟาร์มปลอดภัย"})
        token=auth["token"]
        status,_=self.request("GET","/api/storage/private/accounts",token=token)
        self.assertEqual(status,403)
        status,_=self.request("PUT","/api/storage/private/profile_0877777777",{"value":"{}"},token)
        self.assertEqual(status,403)
        status,_=self.request("PUT","/api/storage/private/profile_0888888888",{"value":"{}"},token)
        self.assertEqual(status,200)

    def test_gis_shared_storage_is_disabled(self):
        _,first=self.request("POST","/api/auth/register",{"phone":"0861111111","password":"strong-pass","name":"ผู้ใช้หนึ่ง","farm":"ฟาร์มหนึ่ง"})
        own=[{"id":"p1","ownerPhone":"0861111111","type":"longan"}]
        status,_=self.request("PUT","/api/storage/shared/plants",{"value":json.dumps(own)},first["token"])
        self.assertEqual(status,403)
        _,second=self.request("POST","/api/auth/register",{"phone":"0862222222","password":"strong-pass","name":"ผู้ใช้สอง","farm":"ฟาร์มสอง"})
        status,_=self.request("PUT","/api/storage/shared/plants",{"value":"[]"},second["token"])
        self.assertEqual(status,403)

    def test_password_reset_request_requires_org_approval(self):
        _,farmer=self.request("POST","/api/auth/register",{"phone":"0833333333","password":"old-password","name":"ผู้ขอรีเซ็ต","farm":"ฟาร์มคำขอ"})
        self.assertEqual(self.request("POST","/api/auth/password-reset-requests",{"phone":"0833333333"})[0],202)
        _,org=self.request("POST","/api/org/auth/login",{"email":"admin@ecobee.go.th","password":"test-admin-password"})
        _,requests=self.request("GET","/api/org/password-reset-requests",token=org["token"])
        pending=next(x for x in requests if x["phone"]=="0833333333")
        self.assertEqual(self.request("POST","/api/org/password-reset-requests/"+pending["id"],{"action":"approve","password":"approved-password"},org["token"])[0],200)
        self.assertEqual(self.request("GET","/api/auth/me",token=farmer["token"])[0],401)
        self.assertEqual(self.request("POST","/api/auth/login",{"phone":"0833333333","password":"approved-password"})[0],200)

    def test_map_data_is_single_relational_source(self):
        _,farmer=self.request("POST","/api/auth/register",{"phone":"0871234567","password":"strong-pass","name":"เจ้าของแผนที่","farm":"ฟาร์มกลาง"})
        _,hive=self.request("POST","/api/hives",{"name":"รังกลาง","species":"cerana","lat":13.5,"lng":99.8},farmer["token"])
        self.request("POST","/api/plants",{"plant_type":"longan","months":[1,2],"geometry":{"type":"point","coords":[13.5,99.8]}},farmer["token"])
        _,data=self.request("GET","/api/map-data",token=farmer["token"])
        self.assertTrue(any(x["id"]==hive["id"] and x["mine"] for x in data["hives"]))
        self.assertEqual(len(data["plants"]),1)

    def test_logout_revokes_session(self):
        _,auth=self.request("POST","/api/auth/register",{"phone":"0855555555","password":"strong-pass","name":"ผู้ทดสอบออก","farm":"ฟาร์มออก"})
        token=auth["token"]
        self.assertEqual(self.request("GET","/api/auth/me",token=token)[0],200)
        self.assertEqual(self.request("POST","/api/auth/logout",{},token)[0],200)
        self.assertEqual(self.request("GET","/api/auth/me",token=token)[0],401)

    def test_harvest_rejects_zero_quantity(self):
        _,auth=self.request("POST","/api/auth/register",{"phone":"0844444444","password":"strong-pass","name":"ผู้ทดสอบผลผลิต","farm":"ฟาร์มผลผลิต"})
        _,hive=self.request("POST","/api/hives",{"name":"รังผลผลิต","species":"cerana","lat":13.5,"lng":99.8},auth["token"])
        status,data=self.request("POST","/api/harvests",{"hive_id":hive["id"],"product":"น้ำผึ้ง","quantity_kg":0},auth["token"])
        self.assertEqual(status,400);self.assertEqual(data["error"],"invalid_quantity")

    def test_database_file_is_not_public(self):
        status,_=self.request("GET","/ecobee.db")
        self.assertEqual(status,404)

    def test_plant_master_approval_and_simple_field_observation(self):
        _,org=self.request("POST","/api/org/auth/login",{"email":"admin@ecobee.go.th","password":"test-admin-password"})
        status,species=self.request("POST","/api/org/plant-species",{
            "code":"rambutan","thai_name":"เงาะ","scientific_name":"Nephelium lappaceum",
            "resource_type":"both","nectar_score":4,"pollen_score":3,"flowering_months":[2,3],
            "source_title":"ข้อมูลทดสอบ","confidence":"medium","status":"draft"
        },org["token"])
        self.assertEqual(status,201);self.assertEqual(species["grade"],"A")
        _,farmer=self.request("POST","/api/auth/register",{"phone":"0801234567","password":"strong-pass","name":"ผู้บันทึกแปลง","farm":"สวนเงาะ"})
        _,before=self.request("GET","/api/plant-species",token=farmer["token"])
        self.assertFalse(any(x["code"]=="rambutan" for x in before))
        self.assertEqual(self.request("PUT","/api/org/plant-species/rambutan",{"status":"approved"},org["token"])[0],200)
        status,plant=self.request("POST","/api/plants",{
            "plant_type":"rambutan","geometry":{"type":"point","coords":[13.5,99.8]},
            "area_rai":2.5,"bloom_status":"starting","pesticide_use":"no"
        },farmer["token"])
        self.assertEqual(status,201)
        _,mapped=self.request("GET","/api/map-data",token=farmer["token"])
        row=next(x for x in mapped["plants"] if x["id"]==plant["id"])
        self.assertEqual(row["area_rai"],2.5);self.assertEqual(row["bloom_status"],"starting")

    def test_organization_management_uses_backend(self):
        _,farmer=self.request("POST","/api/auth/register",{"phone":"0822222222","password":"old-password","name":"เกษตรกรจัดการ","farm":"ฟาร์มจัดการ"})
        _,org=self.request("POST","/api/org/auth/login",{"email":"admin@ecobee.go.th","password":"test-admin-password"})
        token=org["token"]
        self.assertEqual(self.request("POST","/api/org/farmers/0822222222/verification",{"safety":True,"standard":True},token)[0],200)
        _,farmers=self.request("GET","/api/org/farmers",token=token)
        managed=next(x for x in farmers if x["phone"]=="0822222222")
        self.assertTrue(managed["verify"]["safety"]);self.assertTrue(managed["verify"]["standard"])
        self.assertEqual(self.request("POST","/api/org/farmers/0822222222/reset-password",{"password":"new-password"},token)[0],200)
        self.assertEqual(self.request("GET","/api/auth/me",token=farmer["token"])[0],401)
        self.assertEqual(self.request("POST","/api/auth/login",{"phone":"0822222222","password":"new-password"})[0],200)

    def test_admin_create_and_delete(self):
        _,org=self.request("POST","/api/org/auth/login",{"email":"admin@ecobee.go.th","password":"test-admin-password"})
        token=org["token"]
        status,created=self.request("POST","/api/org/admins",{"email":"second@example.com","name":"ผู้ดูแลสอง","password":"second-admin-password"},token)
        self.assertEqual(status,201)
        self.assertEqual(self.request("DELETE",f"/api/org/admins/{created['id']}",token=token)[0],200)

    def test_organization_can_delete_farmer_and_related_data(self):
        _,farmer=self.request("POST","/api/auth/register",{"phone":"0812345678","password":"farmer-password","name":"เกษตรกรลบ","farm":"ฟาร์มลบ"})
        _,hive=self.request("POST","/api/hives",{"name":"รังที่จะลบ","species":"cerana","lat":13.5,"lng":99.8},farmer["token"])
        _,batch=self.request("POST","/api/harvests",{"hive_id":hive["id"],"product":"น้ำผึ้ง","quantity_kg":2},farmer["token"])
        _,org=self.request("POST","/api/org/auth/login",{"email":"admin@ecobee.go.th","password":"test-admin-password"})
        self.assertEqual(self.request("DELETE","/api/org/farmers/0812345678",token=org["token"])[0],200)
        self.assertEqual(self.request("GET","/api/auth/me",token=farmer["token"])[0],401)
        self.assertEqual(self.request("GET","/api/trace/"+batch["batch_code"])[0],404)
        _,farmers=self.request("GET","/api/org/farmers",token=org["token"])
        self.assertFalse(any(x["phone"]=="0812345678" for x in farmers))
        self.assertEqual(self.request("DELETE","/api/org/farmers/0812345678",token=org["token"])[0],404)


if __name__ == "__main__": unittest.main()
