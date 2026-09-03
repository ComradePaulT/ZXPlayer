# Putting ZXPlayer on GitHub with GitHub Desktop

## First publication

1. Create a free account at https://github.com if you do not already have one.
2. Install GitHub Desktop for Windows from https://desktop.github.com and sign in.
3. Extract `ZXPlayer-GitHub.zip` somewhere permanent, for example
   `Documents\GitHub\ZXPlayer`.
4. In GitHub Desktop choose **File > Add local repository**, then select that
   `ZXPlayer` folder.
5. If GitHub Desktop says it is not yet a Git repository, click the offered
   **create a repository** link. Use `ZXPlayer` as the name and `main` as the
   default branch if it asks.
6. Open the **Changes** tab. Confirm that no tape files, downloaded cover art,
   `settings.json` or cache files appear.
7. Enter `Initial ZXPlayer release` in the Summary box and click
   **Commit to main**.
8. Click **Publish repository**. Keep **Keep this code private** selected for
   the first upload. You can make it public later from the repository settings.
9. Choose **Repository > View on GitHub** to see the result.

## Publishing later changes

Copy changed program files into this same Windows folder. GitHub Desktop lists
them under **Changes**. Review the list, type a short summary, click
**Commit to main**, then click **Push origin**.

For an existing private repository, copy the updated files from this package
over the matching files in the local folder shown by GitHub Desktop. Do not
delete the local `.git` folder. Review the changes, commit them with a summary
such as `Add licences and community notices`, then click **Push origin**.

## Before making it public

- Keep cassette files and downloaded artwork out of the repository.
- Confirm that `LICENSE` and `THIRD-PARTY-NOTICES.md` appear on GitHub.
- Check that the README accurately describes the current release.
- Consider using GitHub Releases for downloadable ZIP packages rather than
  adding each generated ZIP to the source repository.
