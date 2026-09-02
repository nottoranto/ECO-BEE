import json, os, re, tempfile, threading, unittest, urllib.error, urllib.parse, urllib.request
from pathlib import Path
from unittest import mock
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

    def test_reference_catalog_is_approved_and_deduplicated(self):
        with server.connect() as db:
            rows=db.execute("SELECT thai_name,status FROM plant_species WHERE created_by IS NULL").fetchall()
        names=[server.normalized_plant_name(row["thai_name"]) for row in rows]
        self.assertEqual(len(names),len(set(names)))
        self.assertEqual(len(names),330)
        self.assertTrue(all(row["status"]=="approved" for row in rows))
        self.assertTrue(all(re.search(r"[ก-๙]",name) and not re.search(r"[A-Za-z0-9]",name) for name in names))
        self.assertIn("ยอดขวัญชันโรง",names)
        for corrected in ("ยางพารา","คุณนายตื่นสาย","ชวนชม","ดาวเรือง","มะตูมแขก","มะม่วงหาวมะนาวโห่","บุหงาส่าหรี","เข็มปัตตาเวีย"):
            self.assertIn(corrected,names)
        for misspelled in ("ขางพารา","คุณนายตื่นสาข","ช่วนชม","ดาวเรื่อง","มะดูมแขก","มะม่วงหาวมะนาวโห","บุหงาส่าหรื","เข็มปิตตาเวีย"):
            self.assertNotIn(misspelled,names)

    def test_phase2_field_schema_is_additive(self):
        with server.connect() as db:
            tables={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        self.assertTrue({"plots","plot_crops","crop_observations","tree_count_jobs"}.issubset(tables))

    def test_mobile_forms_protect_passwords_and_avoid_input_zoom(self):
        root=Path(__file__).resolve().parent
        farmer=(root/"farmer/index.html").read_text()
        organization=(root/"organization/index.html").read_text()
        trace=(root/"trace/index.html").read_text()
        self.assertIn("input,select,textarea{font-size:max(16px",farmer)
        self.assertIn("input,select,textarea{font-size:16px!important;}",organization)
        self.assertIn("input,select,textarea{font-size:16px!important;}",trace)
        for field in ("fp-pass","ra-pass","aa-pass"):
            self.assertIn(f'id="{field}" type="password"',organization)
        self.assertNotIn('id="fp-pass" type="text"',organization)
        self.assertNotIn('id="ra-pass" type="text"',organization)
        self.assertNotIn('id="aa-pass" type="text"',organization)

    def test_map_keeps_bee_markers_above_other_layers_and_uses_plant_icons(self):
        farmer=(Path(__file__).resolve().parent/"farmer/index.html").read_text()
        self.assertIn("map.createPane('hiveMarkerPane')",farmer)
        self.assertIn("style.zIndex=750",farmer)
        self.assertIn("pane:'hiveMarkerPane',zIndexOffset:1000",farmer)
        self.assertIn('<use href="#icon-bee"/>',farmer)
        self.assertIn("map-pin-plant-emoji",farmer)
        self.assertIn("escapeHtml(plant.icon||'🌿')",farmer)
        self.assertIn("icon:plantIconFor(p.code,p.thai_name)",farmer)

    def test_plant_form_calculates_plot_area_and_only_asks_tree_count_for_points(self):
        farmer=(Path(__file__).resolve().parent/"farmer/index.html").read_text()
        self.assertIn("พื้นที่คำนวณอัตโนมัติ",farmer)
        self.assertIn("ในขอบเขตนี้มีพืชประมาณกี่ต้น?",farmer)
        self.assertIn("การเพิ่มแบบจุดจะไม่บันทึกพื้นที่ปลูก",farmer)
        self.assertIn("tree_count:treeCount",farmer)
        self.assertIn("polygonAreaRai(geom.coords)",farmer)
        self.assertNotIn('id="plant-area"',farmer)
        self.assertNotIn('id="plant-research"',farmer)
        self.assertNotIn('ข้อมูลนี้ใช้ช่วยวางแผน แต่ยังไม่ใช้คำนวณจำนวนรัง',farmer)

    def test_distance(self):
        self.assertAlmostEqual(server.haversine(13.5282,99.8134,13.5282,99.8134),0)
        self.assertGreater(server.haversine(13.5282,99.8134,13.5382,99.8134),1)

    def test_geometry_center(self):
        self.assertEqual(server.geometry_center({"type":"point","coords":[13,99]}),(13.0,99.0))
        self.assertEqual(server.geometry_center({"type":"polygon","coords":[[10,20],[12,24]]}),(11,22))

    def test_google_weather_is_normalized_for_farmer_ui(self):
        current={
          "temperature":{"degrees":31.4},"relativeHumidity":68,
          "feelsLikeTemperature":{"degrees":35.1},
          "precipitation":{"qpf":{"quantity":1.25}},
          "weatherCondition":{"type":"RAIN_SHOWERS"},
          "wind":{"speed":{"value":12.3}}
        }
        daily={"forecastDays":[{
          "displayDate":{"year":2026,"month":8,"day":17},
          "maxTemperature":{"degrees":34},"minTemperature":{"degrees":26},
          "daytimeForecast":{"weatherCondition":{"type":"PARTLY_CLOUDY"},"precipitation":{"probability":{"percent":40}}},
          "nighttimeForecast":{"precipitation":{"probability":{"percent":70}}}
        }]}
        data=server.normalize_google_weather(current,daily)
        self.assertEqual(data["source"],"google_weather")
        self.assertEqual(data["current"]["temperature_2m"],31.4)
        self.assertEqual(data["current"]["weather_code"],61)
        self.assertEqual(data["daily"]["time"],["2026-08-17"])
        self.assertEqual(data["daily"]["precipitation_probability_max"],[70])

    def test_places_search_is_private_thai_and_resolves_a_selected_place(self):
        _,auth=self.request("POST","/api/auth/register",{"phone":"0867000002","password":"safe-pass","name":"ผู้ทดสอบค้นหา","farm":"ฟาร์มค้นหา"})
        suggestions=[{"place_id":"thai_place_1","label":"ตลาดน้ำอัมพวา","address":"อำเภออัมพวา สมุทรสงคราม"}]
        details={"place_id":"thai_place_1","label":"ตลาดน้ำอัมพวา","address":"อำเภออัมพวา สมุทรสงคราม","lat":13.425,"lng":99.955}
        with mock.patch.object(server,"GOOGLE_PLACES_API_KEY","server-only-places-key"), \
             mock.patch.object(server,"fetch_google_place_suggestions",return_value=suggestions) as autocomplete, \
             mock.patch.object(server,"fetch_google_place_details",return_value=details) as place_details:
            encoded_query=urllib.parse.quote("ตลาดน้ำอัมพวา")
            status,data=self.request("GET",f"/api/places/autocomplete?q={encoded_query}&lat=13.5&lng=99.8&session_token=session-1",token=auth["token"])
            detail_status,detail=self.request("GET","/api/places/details?place_id=thai_place_1&session_token=session-1",token=auth["token"])
        self.assertEqual(status,200);self.assertEqual(data["results"],suggestions)
        self.assertEqual(detail_status,200);self.assertEqual(detail,details)
        autocomplete.assert_called_once_with("ตลาดน้ำอัมพวา",13.5,99.8,"session-1")
        place_details.assert_called_once_with("thai_place_1","session-1")
        self.assertNotIn("server-only-places-key",json.dumps(data)+json.dumps(detail))

    def test_farmer_search_combines_local_data_with_server_side_google_places(self):
        farmer=(Path(__file__).resolve().parent/"farmer/index.html").read_text()
        self.assertIn("/api/places/autocomplete",farmer)
        self.assertIn("/api/places/details",farmer)
        self.assertIn("includedRegionCodes",Path(__file__).resolve().parent.joinpath("server.py").read_text())
        self.assertIn("ผลการค้นหาสถานที่โดย Google",farmer)
        self.assertNotIn("nominatim.openstreetmap.org/search",farmer)

    def test_farmer_gps_search_uses_a_temporary_blue_marker(self):
        farmer=(Path(__file__).resolve().parent/"farmer/index.html").read_text()
        self.assertIn("ค้นหาสถานที่ หรือพิกัด GPS",farmer)
        self.assertIn("function parseGpsCoordinates(value)",farmer)
        self.assertIn("replace(/^gps\\s*:\\s*/i",farmer)
        self.assertIn("function showTemporarySearchMarker(lat,lng,label)",farmer)
        self.assertIn("background:#2563eb",farmer)
        self.assertIn("หมุดสีน้ำเงินนี้เป็นหมุดชั่วคราวและไม่ได้บันทึกลงระบบ",farmer)
        self.assertIn("if(temporarySearchMarker)map.removeLayer(temporarySearchMarker)",farmer)

    def test_weather_proxy_requires_farmer_login_and_keeps_key_server_side(self):
        self.assertEqual(self.request("GET","/api/weather?lat=13.5&lng=99.8")[0],401)
        _,auth=self.request("POST","/api/auth/register",{"phone":"0867000001","password":"safe-pass","name":"ผู้ทดสอบอากาศ","farm":"ฟาร์มอากาศ"})
        sample={"source":"google_weather","current":{"temperature_2m":30},"daily":{"time":[]}}
        with mock.patch.object(server,"GOOGLE_WEATHER_API_KEY","server-only-test-key"), mock.patch.object(server,"fetch_google_weather",return_value=sample) as fetch:
            status,data=self.request("GET","/api/weather?lat=13.5&lng=99.8",token=auth["token"])
        self.assertEqual(status,200)
        self.assertEqual(data,sample)
        fetch.assert_called_once_with(13.5,99.8)
        self.assertNotIn("server-only-test-key",json.dumps(data))

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

    def test_farmer_can_drag_only_their_own_hive(self):
        _,first=self.request("POST","/api/auth/register",{"phone":"0861110001","password":"safe-pass","name":"เจ้าของรัง","farm":"ฟาร์มหนึ่ง"})
        _,second=self.request("POST","/api/auth/register",{"phone":"0861110002","password":"safe-pass","name":"ผู้อื่น","farm":"ฟาร์มสอง"})
        _,hive=self.request("POST","/api/hives",{"name":"รังลากได้","species":"cerana","lat":13.5,"lng":99.8},first["token"])
        self.assertEqual(self.request("PUT","/api/hives/"+hive["id"],{"lat":13.51,"lng":99.81},second["token"])[0],404)
        status,moved=self.request("PUT","/api/hives/"+hive["id"],{"lat":13.51,"lng":99.81},first["token"])
        self.assertEqual(status,200);self.assertEqual((moved["lat"],moved["lng"]),(13.51,99.81))

    def test_farm_boundary_is_private_and_owned_by_farmer(self):
        _,first=self.request("POST","/api/auth/register",{"phone":"0871000001","password":"safe-pass","name":"เจ้าของพื้นที่","farm":"สวนหนึ่ง"})
        _,second=self.request("POST","/api/auth/register",{"phone":"0871000002","password":"safe-pass","name":"เกษตรกรอื่น","farm":"สวนสอง"})
        geometry={"type":"polygon","coords":[[13.50,99.80],[13.50,99.81],[13.51,99.81],[13.51,99.80]]}
        status,boundary=self.request("POST","/api/farm-boundaries",{"name":"แปลงส่วนตัว","geometry":geometry,"target_species":"cerana","min_spacing_km":0.7},first["token"])
        self.assertEqual(status,201)
        _,own_map=self.request("GET","/api/map-data",token=first["token"])
        _,other_map=self.request("GET","/api/map-data",token=second["token"])
        self.assertEqual([x["id"] for x in own_map["farm_boundaries"]],[boundary["id"]])
        self.assertEqual(other_map["farm_boundaries"],[])
        self.assertEqual(self.request("DELETE","/api/farm-boundaries/"+boundary["id"],token=second["token"])[0],404)
        self.assertEqual(self.request("DELETE","/api/farm-boundaries/"+boundary["id"],token=first["token"])[0],200)

    def test_public_trace_uses_real_aggregates_without_coordinates(self):
        _,auth=self.request("POST","/api/auth/register",{"phone":"0891111111","password":"safe-pass","name":"ผู้ทดสอบสิ่งแวดล้อม","farm":"ฟาร์มข้อมูลจริง"})
        token=auth["token"]
        _,hive=self.request("POST","/api/hives",{"name":"รังข้อมูลจริง","species":"cerana","lat":13.5,"lng":99.8},token)
        self.request("POST","/api/plants",{"plant_type":"longan","variety":"อีดอ","months":[1,2,3],"tree_count":8,"geometry":{"type":"point","coords":[13.501,99.801]}},token)
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
        self.request("POST","/api/plants",{"plant_type":"longan","months":[1,2],"tree_count":3,"geometry":{"type":"point","coords":[13.5,99.8]}},farmer["token"])
        _,data=self.request("GET","/api/map-data",token=farmer["token"])
        self.assertTrue(any(x["id"]==hive["id"] and x["mine"] for x in data["hives"]))
        self.assertEqual(len(data["plants"]),1)

    def test_z_farmer_map_uses_viewport_and_never_exposes_other_hives(self):
        _,owner=self.request("POST","/api/auth/register",{"phone":"0871234501","password":"strong-pass","name":"เจ้าของมุมมอง","farm":"สวนหนึ่ง"})
        _,other=self.request("POST","/api/auth/register",{"phone":"0871234502","password":"strong-pass","name":"เกษตรกรอื่น","farm":"สวนสอง"})
        _,other_hive=self.request("POST","/api/hives",{"name":"รังส่วนตัว","species":"cerana","lat":13.51,"lng":99.81},other["token"])
        _,near=self.request("POST","/api/plants",{"plant_type":"longan","tree_count":2,"geometry":{"type":"point","coords":[13.51,99.81]}},other["token"])
        _,far=self.request("POST","/api/plants",{"plant_type":"longan","tree_count":4,"geometry":{"type":"point","coords":[18.7,98.9]}},other["token"])
        status,data=self.request("GET","/api/map-data?bounds=13.4,99.7,13.6,99.9",token=owner["token"])
        self.assertEqual(status,200)
        self.assertFalse(any(x["id"]==other_hive["id"] for x in data["hives"]))
        self.assertTrue(any(x["id"]==near["id"] for x in data["plants"]))
        self.assertFalse(any(x["id"]==far["id"] for x in data["plants"]))

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
            "area_rai":2.5,"tree_count":5,"bloom_status":"starting","pesticide_use":"no"
        },farmer["token"])
        self.assertEqual(status,201)
        _,mapped=self.request("GET","/api/map-data",token=farmer["token"])
        row=next(x for x in mapped["plants"] if x["id"]==plant["id"])
        self.assertIsNone(row["area_rai"]);self.assertEqual(row["tree_count"],5)
        self.assertEqual(row["bloom_status"],"starting")

        status,missing=self.request("POST","/api/plants",{
            "plant_type":"rambutan","geometry":{"type":"point","coords":[13.5,99.8]}
        },farmer["token"])
        self.assertEqual(status,400);self.assertEqual(missing["error"],"tree_count_required")

        status,polygon=self.request("POST","/api/plants",{
            "plant_type":"rambutan","tree_count":120,"area_rai":999,
            "geometry":{"type":"polygon","coords":[[13.5,99.8],[13.5,99.801],[13.501,99.801],[13.501,99.8]]}
        },farmer["token"])
        self.assertEqual(status,201)
        self.assertGreater(polygon["area_rai"],7);self.assertLess(polygon["area_rai"],8)
        _,mapped=self.request("GET","/api/map-data",token=farmer["token"])
        polygon_row=next(x for x in mapped["plants"] if x["id"]==polygon["id"])
        self.assertAlmostEqual(polygon_row["area_rai"],polygon["area_rai"],places=6)
        self.assertEqual(polygon_row["tree_count"],120)

    def test_research_calendar_is_seeded_without_fake_nectar_quantity(self):
        _,farmer=self.request("POST","/api/auth/register",{"phone":"0807654321","password":"strong-pass","name":"ผู้ใช้ข้อมูลวิจัย","farm":"สวนวิจัย"})
        status,rows=self.request("GET","/api/plant-species",token=farmer["token"])
        self.assertEqual(status,200)
        self.assertGreaterEqual(len(rows),35)
        mango=next(x for x in rows if x["code"]=="mango")
        self.assertEqual(mango["scientific_name"],"Mangifera indica")
        self.assertEqual(mango["resource_type"],"both")
        self.assertEqual(mango["flowering_months"],[1,2,11,12])
        self.assertEqual(mango["grade"],"unrated")
        self.assertIsNone(mango["nectar_amount"])
        self.assertIn("Bee Flora",mango["source_title"])

    def test_pollination_research_guidance_requires_login(self):
        self.assertEqual(self.request("GET","/api/pollination-guidance?plant_code=passion_fruit")[0],401)
        _,farmer=self.request("POST","/api/auth/register",{"phone":"0807654322","password":"strong-pass","name":"ผู้ใช้คำแนะนำ","farm":"สวนเสาวรส"})
        status,rows=self.request("GET","/api/pollination-guidance?plant_code=passion_fruit",token=farmer["token"])
        self.assertEqual(status,200);self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]["evidence"]["fruit_set_with_bees_percent"],91.66)
        self.assertNotIn("lat",json.dumps(rows))

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
