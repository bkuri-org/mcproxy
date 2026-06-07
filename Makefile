.PHONY: deploy

# === Deploy mcproxy: push to GitHub, then trigger server2 deploy ===
deploy:
	@echo "=== Deploying mcproxy from server1 → server2 ==="
	git push
	ssh server2-auto 'cd /srv/containers/mcproxy && make deploy'
	@echo "✅ Deploy complete"